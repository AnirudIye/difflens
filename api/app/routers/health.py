from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, str]:
    # ponytail: always reports ready; gains a database ping when the DB layer lands on Day 2
    return {"status": "ready"}
