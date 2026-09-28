from sqlalchemy import select
from .db import SessionLocal
from .models import TestCase, TestCaseRisk
from .basic_cases import CODES, definition

def seed():
    with SessionLocal() as db:
        for code in CODES:
            d=definition(code)
            case=db.scalar(select(TestCase).where(TestCase.code==code))
            if case is None:
                case=TestCase(code=code)
                db.add(case)
            case.name=d['name'];case.category='basic'
            case.description='Version '+d['version']+'; '+', '.join(d['assertions'])
            case.command='python3 '+d['entrypoint']+' --input <case-input.json> --output-dir <attempt-dir>'
            case.timeout_seconds=d['timeout_seconds'];case.expected_exit_code=0
            case.risk_level=TestCaseRisk.READ_ONLY if d['risk_level']=='read_only' else TestCaseRisk.STATE_CHANGE
            case.enabled=True
        db.commit()
if __name__=='__main__': seed()


def install_missing(db):
    """Add shipped cases without rewriting operator settings or historical IDs."""
    added=[]
    for code in CODES:
        if db.scalar(select(TestCase).where(TestCase.code==code)) is not None:
            continue
        d=definition(code)
        db.add(TestCase(code=code,name=d['name'],category='basic',
            description='Version '+d['version']+'; '+', '.join(d['assertions']),
            command='python3 '+d['entrypoint']+' --input <case-input.json> --output-dir <attempt-dir>',
            timeout_seconds=d['timeout_seconds'],expected_exit_code=0,
            risk_level=TestCaseRisk.READ_ONLY if d['risk_level']=='read_only' else TestCaseRisk.STATE_CHANGE,
            enabled=True))
        added.append(code)
    db.flush()
    return added
