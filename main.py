import base64
import binascii
import io
import logging
import os
import zipfile as zf
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

logger = logging.getLogger("fusion_ess_extractor")

app = FastAPI(title="Fusion Account Analysis Report Extractor")


class ReportRequest(BaseModel):
    document_content: str = Field(
        ...,
        description="Base64-encoded ZIP DocumentContent for Account Analysis Report.",
    )


def _to_number(value):
    if value is None:
        return None

    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _decode_base64(document_content: str) -> bytes:
    logger.info(
        "Decoding base64 document content (%d chars)",
        len(document_content)
    )

    try:
        decoded = base64.b64decode(
            document_content,
            validate=True
        )
    except (binascii.Error, ValueError) as exc:
        logger.error(
            "Failed to decode base64 document content: %s",
            exc
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid base64 document_content: {exc}"
        )

    logger.info(
        "Decoded document content: %d bytes",
        len(decoded)
    )

    return decoded


def _find_xml_entry(zip_file: zf.ZipFile) -> str:
    for name in zip_file.namelist():
        if name.lower().endswith(".xml"):
            return name

    raise HTTPException(
        status_code=422,
        detail="No XML file found inside ZIP"
    )


def _validate_account_analysis(
    zip_file: zf.ZipFile,
    xml_name: str
) -> None:

    with zip_file.open(xml_name) as stream:
        head = stream.read(65536)

    head_text = head.decode(
        "utf-8",
        errors="replace"
    )

    if "XLAAARPT" not in head_text:
        raise HTTPException(
            status_code=422,
            detail="XML does not contain XLAAARPT. Expected Account Analysis Report."
        )

    logger.info(
        "Detected Account Analysis Report (XLAAARPT)"
    )


def _parse_ccid(elem) -> dict:

    code_combination = elem.findtext(
        ".//ACCOUNTING_CODE_COMBINATION"
    )

    begin_dr = _to_number(
        elem.findtext(".//ACCT_SUM_BAL_DR")
    ) or 0.0

    begin_cr = _to_number(
        elem.findtext(".//ACCT_SUM_BAL_CR")
    ) or 0.0

    period_dr = _to_number(
        elem.findtext(".//ACCT_SUM_PR_DR")
    ) or 0.0

    period_cr = _to_number(
        elem.findtext(".//ACCT_SUM_PR_CR")
    ) or 0.0

    begin_net = begin_dr - begin_cr

    if begin_net >= 0:
        begin_balance_dr = begin_net
        begin_balance_cr = 0.0
    else:
        begin_balance_dr = 0.0
        begin_balance_cr = -begin_net

    period_net = period_dr - period_cr

    ending_net = begin_net + period_net

    if ending_net >= 0:
        ending_balance_dr = ending_net
        ending_balance_cr = 0.0
    else:
        ending_balance_dr = 0.0
        ending_balance_cr = -ending_net

    items = []

    for jeline in elem.findall(".//JELINE_ROW"):

        source = (
            jeline.findtext(".//JE_SOURCE_NAME")
            or jeline.findtext(".//APPLICATION_NAME")
        )

        number = (
            jeline.findtext(".//TRANSACTION_NUMBER")
            or jeline.findtext(".//DOCUMENT_SEQUENCE_NUMBER")
        )

        debit = _to_number(
            jeline.findtext(".//ACCOUNTED_DR")
        ) or 0.0

        credit = _to_number(
            jeline.findtext(".//ACCOUNTED_CR")
        ) or 0.0

        items.append({
            "source": source,
            "number": number,
            "debitBalance": round(debit, 2),
            "creditBalance": round(credit, 2)
        })

    return {
        "codeCombination": code_combination,
        "beginBalance_debit": round(begin_balance_dr, 2),
        "beginBalance_credit": round(begin_balance_cr, 2),
        "periodBalance_debit": round(period_dr, 2),
        "periodBalance_credit": round(period_cr, 2),
        "endingBalance_debit": round(ending_balance_dr, 2),
        "endingBalance_credit": round(ending_balance_cr, 2),
        "items": items
    }


def _stream_parse_account_analysis(stream) -> list:

    results = []

    found_any = False

    for event, elem in ET.iterparse(
        stream,
        events=("end",)
    ):

        if elem.tag != "CCID_S":
            continue

        found_any = True

        record = _parse_ccid(elem)

        results.append(record)

        elem.clear()

    if not found_any:
        logger.error(
            "Account Analysis XML had no CCID_S rows"
        )

        raise HTTPException(
            status_code=422,
            detail="Account Analysis XML had no CCID_S rows"
        )

    logger.info(
        "Parsed %d Account Analysis records",
        len(results)
    )

    return results


def _get_account_analysis_records(
    document_content: str
):

    zip_bytes = _decode_base64(
        document_content
    )

    try:

        with zf.ZipFile(
            io.BytesIO(zip_bytes),
            "r"
        ) as zip_file:

            xml_name = _find_xml_entry(
                zip_file
            )

            logger.info(
                "Using XML file: %s",
                xml_name
            )

            _validate_account_analysis(
                zip_file,
                xml_name
            )

            with zip_file.open(
                xml_name
            ) as stream:

                records = _stream_parse_account_analysis(
                    stream
                )

    except zf.BadZipFile as exc:

        logger.error(
            "Invalid ZIP document: %s",
            exc
        )

        raise HTTPException(
            status_code=422,
            detail="DocumentContent is not a valid ZIP file"
        )

    return records


@app.post("/report")
def get_report(payload: ReportRequest):

    logger.info(
        "Received Account Analysis Report request"
    )

    records = _get_account_analysis_records(
        payload.document_content
    )

    items_count = sum(
        len(record.get("items", []))
        for record in records
    )

    logger.info(
        "Returning %d Account Analysis records",
        len(records)
    )

    return JSONResponse(
        content={
            "report_name": "Account Analysis Report",
            "count": len(records),
            "items_count": items_count,
            "data": records
        }
    )

