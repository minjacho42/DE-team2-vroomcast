from airflow import DAG
from airflow.providers.amazon.aws.operators.emr import (
    EmrCreateJobFlowOperator, EmrTerminateJobFlowOperator)
from airflow.providers.amazon.aws.sensors.emr import EmrJobFlowSensor
from airflow.utils.context import Context

from modules.constants import CARS, S3_BUCKET, S3_CONFIG_BUCKET
from utils.time import pull_time_info
from utils.xcom import pull_from_xcom

# EMR 클러스터 설정을 위한 상수
EMR_CONFIG = {
    "RELEASE_LABEL": "emr-7.7.0",  # EMR 버전
    "INSTANCE_TYPE_MASTER": "m5.xlarge",  # 마스터 노드 인스턴스 타입
    "INSTANCE_TYPE_CORE": "m5.xlarge",  # 코어 노드 인스턴스 타입
    "CORE_INSTANCE_COUNT": 2,  # 코어 노드 인스턴스 개수
    "APPLICATIONS": [  # 사용할 애플리케이션
        {"Name": "Hadoop"},
        {"Name": "Hive"},
        {"Name": "JupyterEnterpriseGateway"},
        {"Name": "Livy"},
        {"Name": "Spark"},
    ],
    "AUTO_TERMINATION_IDLE_TIMEOUT": 1800,  # 클러스터 자동 종료 시간
}


def get_emr_job_flow_overrides():  # EMR 클러스터 설정 반환
    return {
        "Name": "mainTransformCluster",
        "LogUri": "{{ var.value.emr_base_log_uri }}/{{ ts_nodash }}/",
        "ReleaseLabel": EMR_CONFIG["RELEASE_LABEL"],
        "ServiceRole": "{{ var.value.emr_service_role }}",
        "JobFlowRole": "{{ var.value.emr_ec2_role }}",
        "Instances": {
            "Ec2SubnetId": "{{ var.value.emr_subnet_id }}",
            "Ec2KeyName": "{{ var.value.emr_key_pair }}",
            "EmrManagedMasterSecurityGroup": "{{ var.value.emr_master_sg }}",
            "EmrManagedSlaveSecurityGroup": "{{ var.value.emr_slave_sg }}",
            "InstanceGroups": [
                {
                    "InstanceCount": 1,
                    "InstanceRole": "MASTER",
                    "Name": "Primary",
                    "InstanceType": EMR_CONFIG["INSTANCE_TYPE_MASTER"],
                },
                {
                    "InstanceCount": EMR_CONFIG["CORE_INSTANCE_COUNT"],
                    "InstanceRole": "CORE",
                    "Name": "Core",
                    "InstanceType": EMR_CONFIG["INSTANCE_TYPE_CORE"],
                },
            ],
        },
        "Applications": EMR_CONFIG["APPLICATIONS"],
        "BootstrapActions": [
            {
                "Name": "kiwi-bootstrap",
                "ScriptBootstrapAction": {
                    "Path": f"s3://{S3_CONFIG_BUCKET}/"
                    + "{{ var.value.emr_bootstrap_script_path }}"
                },
            }
        ],
        "AutoTerminationPolicy": {
            "IdleTimeout": EMR_CONFIG["AUTO_TERMINATION_IDLE_TIMEOUT"]
        },
        "Steps": [  # EMR 클러스터에서 실행할 스텝 목록 (각 스텝은 하나의 Job Flow)
            {  # 정적 데이터 변환 Spark Job
                "Name": "Run Transform Static Spark Job",
                "ActionOnFailure": "CONTINUE",
                "HadoopJarStep": {
                    "Jar": "command-runner.jar",
                    "Args": [
                        "spark-submit",
                        "--deploy-mode",
                        "cluster",
                        f"s3://{S3_CONFIG_BUCKET}/"
                        + "{{ var.value.emr_static_script_path }}",
                        "--bucket",
                        f"{S3_BUCKET}",
                        "--input_post_paths",
                        "combined/*/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['batch'] }}/static/post*.parquet",
                        "--input_comment_paths",
                        "combined/*/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['batch'] }}/static/comment*.parquet",
                        "--output_dir",
                        "transformed/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['batch'] }}/",
                    ],
                },
            },
            {  # 동적 데이터 변환 Spark Job
                "Name": "Run Transform Dynamic Spark Job",
                "ActionOnFailure": "CONTINUE",
                "HadoopJarStep": {
                    "Jar": "command-runner.jar",
                    "Args": [
                        "spark-submit",
                        "--deploy-mode",
                        "cluster",
                        f"s3://{S3_CONFIG_BUCKET}/"
                        + "{{ var.value.emr_dynamic_script_path }}",
                        "--bucket",
                        f"{S3_BUCKET}",
                        "--before_dynamic_posts",
                        *[
                            f"combined/{car_id}/"
                            + "{{ task_instance.xcom_pull(task_ids='synchronize', key='prev_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='prev_batch_info')['batch'] }}/dynamic/post_*.parquet"
                            for car_id in CARS
                        ],
                        "--after_dynamic_posts",
                        *[
                            f"combined/{car_id}/"
                            + "{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['batch'] }}/dynamic/post_*.parquet"
                            for car_id in CARS
                        ],
                        "--before_dynamic_comments",
                        *[
                            f"combined/{car_id}/"
                            + "{{ task_instance.xcom_pull(task_ids='synchronize', key='prev_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='prev_batch_info')['batch'] }}/dynamic/comment_*.parquet"
                            for car_id in CARS
                        ],
                        "--after_dynamic_comments",
                        *[
                            f"combined/{car_id}/"
                            + "{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['date'] }}/{{ task_instance.xcom_pull(task_ids='synchronize', key='current_batch_info')['batch'] }}/dynamic/comment_*.parquet"
                            for car_id in CARS
                        ],
                    ],
                },
            },
        ],
    }


def create_execute_emr_task(dag: DAG) -> EmrCreateJobFlowOperator:
    """
    EMR 클러스터 생성 Task를 생성합니다.
    Args:
        dag (DAG): Airflow DAG
    Returns:
        EmrCreateJobFlowOperator: Task
    """
    return EmrCreateJobFlowOperator(
        task_id="create_emr_cluster",
        job_flow_overrides=get_emr_job_flow_overrides(),
        dag=dag,
    )


def create_check_emr_termination_task(dag: DAG) -> EmrJobFlowSensor:
    """
    EMR 클러스터 종료 여부 확인 Task를 생성합니다.
    Args:
        dag (DAG): Airflow DAG
    Returns:
        EmrJobFlowSensor: Task
    """
    return EmrJobFlowSensor(
        task_id="check_emr_termination",
        job_flow_id="{{ task_instance.xcom_pull('create_emr_cluster') }}",
        target_states=["TERMINATED", "TERMINATED_WITH_ERRORS"],
        dag=dag,
    )


def create_terminate_emr_cluster_task(dag: DAG) -> EmrTerminateJobFlowOperator:
    """
    EMR 클러스터 종료 Task를 생성합니다.
    Args:
        dag (DAG): Airflow DAG
    Returns:
        EmrTerminateJobFlowOperator: Task
    """
    return EmrTerminateJobFlowOperator(
        task_id="terminate_emr_cluster",
        job_flow_id="{{ task_instance.xcom_pull('create_emr_cluster') }}",
        dag=dag,
    )
