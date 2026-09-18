import base64
import binascii
import io
import logging
import os
import zipfile as zf
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Fusion Account Analysis Report Extractor"
)


class ReportRequest(BaseModel):
    request_id: int


FUSION_BASE_URL = os.getenv(
    "FUSION_BASE_URL",
    "https://iaaley-test.fa.ocs.oraclecloud.com"
)

FUSION_USERNAME = os.getenv("FUSION_USERNAME")
FUSION_PASSWORD = os.getenv("FUSION_PASSWORD")


def _to_number(value):
    if value is None:
        return 0.0

    value = str(value).strip()

    if not value:
        return 0.0

    value = value.replace(",", "")

    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _decode_base64(document_content):
    if not document_content:
        raise ValueError("DocumentContent is empty")

    if isinstance(document_content, bytes):
        document_content = document_content.decode("utf-8")

    document_content = document_content.strip()

    try:
        return base64.b64decode(
            document_content,
            validate=False
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"Invalid Base64 DocumentContent: {exc}"
        ) from exc


def _find_xml_entry(zip_file):
    xml_files = [
        name
        for name in zip_file.namelist()
        if name.lower().endswith(".xml")
    ]

    if not xml_files:
        raise ValueError("No XML file found inside the downloaded ZIP")

    return xml_files[0]


def _parse_ccid(elem):
    code_combination = (
        elem.findtext("ACCOUNTING_CODE_COMBINATION")
        or ""
    ).strip()

    beginning_debit = _to_number(
        elem.findtext("ACCT_SUM_BAL_DR")
    )

    beginning_credit = _to_number(
        elem.findtext("ACCT_SUM_BAL_CR")
    )

    period_debit = _to_number(
        elem.findtext("ACCT_SUM_PR_DR")
    )

    period_credit = _to_number(
        elem.findtext("ACCT_SUM_PR_CR")
    )

    ending_debit = beginning_debit + period_debit
    ending_credit = beginning_credit + period_credit

    items = []

    for line in elem.findall(".//JELINE_ROW"):
        source = (
            line.findtext("JE_SOURCE_NAME")
            or line.findtext("APPLICATION_NAME")
            or ""
        ).strip()

        transaction_number = (
            line.findtext("TRANSACTION_NUMBER")
            or line.findtext("DOCUMENT_SEQUENCE_NUMBER")
            or ""
        ).strip()

        accounted_debit = _to_number(
            line.findtext("ACCOUNTED_DR")
        )

        accounted_credit = _to_number(
            line.findtext("ACCOUNTED_CR")
        )

        items.append({
            "source": source,
            "transactionNumber": transaction_number,
            "accountedDebit": accounted_debit,
            "accountedCredit": accounted_credit
        })

    return {
        "codeCombination": code_combination,
        "beginningDebit": beginning_debit,
        "beginningCredit": beginning_credit,
        "periodDebit": period_debit,
        "periodCredit": period_credit,
        "endingDebit": ending_debit,
        "endingCredit": ending_credit,
        "items": items
    }


def _stream_parse_account_analysis(xml_stream):
    results = []

    for event, elem in ET.iterparse(
        xml_stream,
        events=("end",)
    ):
        if elem.tag == "CCID_S":
            record = _parse_ccid(elem)

            if record["codeCombination"]:
                results.append(record)

            elem.clear()

    return results


def _fetch_document_content(request_id):
    if not FUSION_USERNAME or not FUSION_PASSWORD:
        raise RuntimeError(
            "FUSION_USERNAME and FUSION_PASSWORD "
            "must be configured"
        )

    url = (
        f"{FUSION_BASE_URL}"
        "/fscmRestApi/resources/11.13.18.05/erpintegrations"
    )

    params = {
        "fields": "DocumentContent",
        "finder": (
            f"ESSJobExecutionDetailsRF;"
            f"requestId={request_id},"
            f"fileType=ALL"
        )
    }

    logger.info(
        "Fetching DocumentContent for ESS request ID %s",
        request_id
    )

    response = requests.get(
        url,
        params=params,
        auth=(FUSION_USERNAME, FUSION_PASSWORD),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json"
        },
        timeout=180
    )

    if response.status_code != 200:
        logger.error(
            "Fusion API failed. Status=%s Response=%s",
            response.status_code,
            response.text[:1000]
        )

        raise HTTPException(
            status_code=response.status_code,
            detail={
                "message": "Failed to fetch DocumentContent from Oracle Fusion",
                "fusion_status": response.status_code,
                "fusion_response": response.text[:1000]
            }
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Oracle Fusion returned a non-JSON response"
        ) from exc

    document_content = data.get("DocumentContent")

    if not document_content:
        items = data.get("items", [])

        if items:
            document_content = items[0].get(
                "DocumentContent"
            )

    if not document_content:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "DocumentContent not found",
                "request_id": request_id,
                "response": data
            }
        )

    return document_content


def _get_account_analysis_records(request_id):
    document_content = _fetch_document_content(
        request_id
    )

    zip_bytes = _decode_base64(
        document_content
    )

    try:
        with zf.ZipFile(
            io.BytesIO(zip_bytes)
        ) as zip_file:

            xml_entry = _find_xml_entry(
                zip_file
            )

            logger.info(
                "Processing XML file: %s",
                xml_entry
            )

            with zip_file.open(
                xml_entry
            ) as xml_stream:

                records = (
                    _stream_parse_account_analysis(
                        xml_stream
                    )
                )

    except zf.BadZipFile as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "DocumentContent was decoded successfully "
                "but is not a valid ZIP file"
            )
        ) from exc

    return records


@app.get("/")
def root():
    return {
        "status": "running",
        "service": "Fusion Account Analysis Report Extractor"
    }


@app.post("/report")
def report(request: ReportRequest):
    try:
        records = _get_account_analysis_records(
            request.request_id
        )

        items_count = sum(
            len(record["items"])
            for record in records
        )

        return {
            "report_name": "Account Analysis Report",
            "request_id": request.request_id,
            "count": len(records),
            "items_count": items_count,
            "data": records
        }

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "Unexpected error processing request ID %s",
            request.request_id
        )

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        ) from exc
