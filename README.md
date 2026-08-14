# CD-Platform

A civic data platform built with Apache Airflow, PostgreSQL and FastAPI.

## Architecture

```mermaid
graph TD
    A["Congress.gov API<br/><br/>Members<br/>Bills<br/>House Votes"] -->|Scheduled ETL| B["cd-etl<br/><br/>• Airflow DAGs<br/>• Fetch API data<br/>• Normalize<br/>• Upsert into database"]
    E["unitedstates/congress-legislators<br/><br/>legislators-current.yaml"] -->|Crosswalk| B
    B --> C[("PostgreSQL<br/><br/>congresses<br/>members<br/>member_terms<br/>bills<br/>bill_subjects<br/>roll_calls<br/>roll_call_member_votes")]
    C -->|Read Queries| D["cd-api<br/><br/>• REST API"]
```
