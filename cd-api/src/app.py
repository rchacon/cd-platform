from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from mangum import Mangum

from db import fetch_current_member_terms
from transform import group_representatives

app = FastAPI()


@app.get("/representatives")
def get_representatives(
    state: str = Query(..., min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"),
    district: int = Query(..., ge=0),
) -> dict:
    rows = fetch_current_member_terms(state.upper(), district)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No data found for state {state.upper()}")
    return group_representatives(rows)


handler = Mangum(app)
