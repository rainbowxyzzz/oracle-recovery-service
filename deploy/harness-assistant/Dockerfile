FROM python:3.12-slim-bookworm
WORKDIR /opt/planner
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home planner
COPY app.py cordis.yml ./
USER 10001:10001
EXPOSE 8099
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8099", "--workers", "1", "--no-access-log"]
