FROM python:3.11-bullseye

RUN apt-get update && apt-get install -y libgl1 && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip
RUN pip install poetry
RUN poetry config virtualenvs.create false
WORKDIR /app
COPY . /app

# ENV PYTHONUNBUFFERED 1

RUN poetry install --no-interaction --with prod
RUN rm -rf /root/.cache/pypoetry
COPY poetry.lock pyproject.toml /code/

CMD ["gunicorn", "-w", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "fishsense_services.app:app", "--reload", "--timeout", "300"]
