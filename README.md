# CD-Platform

Backend for `cd-lookup` WordPress Plugin

## Architecture

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│     Congress API     │ ──▶ │        cd-etl        │ ──▶ │      PostgreSQL      │ ──▶ │        cd-api        │
│  (api.congress.gov)  │     │   (Apache Airflow)   │     │                      │     │  (FastAPI + Mangum)  │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```
