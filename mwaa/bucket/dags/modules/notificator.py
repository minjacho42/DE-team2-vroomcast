import json
import logging
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator
from modules.constants import SLACK_WEBHOOK_URL
from utils.time import pull_time_info

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _generate_community_stats_message(
    stats: dict[str, dict], time_info: dict[str, str | int]
) -> str:
    # 커뮤니티별 통계를 저장할 딕셔너리 초기화
    sums = {
        "bobaedream": {"attempted": 0, "extracted": 0},
        "clien": {"attempted": 0, "extracted": 0},
        "dcinside": {"attempted": 0, "extracted": 0},
    }

    for stat in stats.values():
        for community, results in stat.items():
            if results["attempted_posts_count"] is not None:
                sums[community]["attempted"] += results["attempted_posts_count"]
            if results["extracted_posts_count"] is not None:
                sums[community]["extracted"] += results["extracted_posts_count"]

    # 메시지 생성
    message = ">>>*데이터 수집 리포트*\n"
    message += f"━━━━━━━━━━━━━━━━━━━━━━\n\n"

    message += f">>*수집 정보*\n"
    message += f"📅 일자: {time_info['date']}\n"
    message += f"⏰ 시각: {time_info['time']}\n"
    message += f"🔄 배치: #{time_info['batch']}\n\n"

    message += f">>*커뮤니티별 현황*\n"
    for community, counts in sums.items():
        success_rate = (
            (counts["extracted"] / counts["attempted"] * 100)
            if counts["attempted"] > 0
            else 0
        )
        message += f"🌐 *{community}*\n"
        message += f"└ 시도: `{counts['attempted']:,}건` | 성공: `{counts['extracted']:,}건` | 성공률: `{success_rate:.1f}%`\n"

    message += "\n💡 _상세 내역은 대시보드를 참고해주세요_"

    return message


def create_notificate_extract_task(dag: DAG) -> PythonOperator:
    def _notificate(**context) -> None:
        task_instance = context["task_instance"]
        stats = task_instance.xcom_pull(task_ids="aggregate_task")

        time_info = pull_time_info(**context)

        logger.info("Sending notification to Slack")
        message = _generate_community_stats_message(stats, time_info)
        requests.post(SLACK_WEBHOOK_URL, json={"text": message})
        logger.info("Notification sent to Slack")

    notificate_extract_task = PythonOperator(
        task_id="notificate_extract_task", python_callable=_notificate, dag=dag
    )
    return notificate_extract_task


def create_notificate_all_done_task(dag: DAG) -> PythonOperator:
    def _notificate(**context) -> None:
        logger.info("Sending notification to Slack")
        requests.post(
            SLACK_WEBHOOK_URL, json={"text": f"데이터 처리가 완료되었습니다."}
        )
        logger.info("Notification sent to Slack")

    notificate_all_done_task = PythonOperator(
        task_id="notificate_all_done_task", python_callable=_notificate, dag=dag
    )
    return notificate_all_done_task
