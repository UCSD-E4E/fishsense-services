FROM python:3.11-bullseye
RUN pip install --upgrade pip
RUN pip install poetry
RUN poetry config virtualenvs.create false
WORKDIR /app
COPY . /app

ENV PYTHONUNBUFFERED 1

RUN poetry install --no-interaction --with prod
RUN rm -rf /root/.cache/pypoetry

CMD ["gunicorn", "-w", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "fishsense_services.app:app"]
