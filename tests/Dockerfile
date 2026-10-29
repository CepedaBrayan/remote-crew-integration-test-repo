FROM python:3.12-slim
WORKDIR /tests
COPY app/requirements.txt app/requirements.txt
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements-test.txt
COPY tests/ tests/
