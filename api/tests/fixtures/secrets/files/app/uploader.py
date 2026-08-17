"""Uploads generated report files to object storage."""


def build_object_key(report_id, created_on):
    return f"reports/{created_on:%Y/%m/%d}/{report_id}.pdf"


def content_type_for(filename):
    if filename.endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"
