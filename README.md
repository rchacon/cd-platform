# CD-Platform

Backend for `cd-lookup` WordPress Plugin

## Architecture

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│     Congress API     │ ──▶ │        cd-etl        │ ──▶ │      PostgreSQL      │
│  (api.congress.gov)  │     │   (Apache Airflow)   │     │                      │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```
