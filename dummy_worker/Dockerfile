FROM python:3.12-slim

WORKDIR /app

# install deps
RUN pip install fastapi uvicorn

# copy app
COPY * /app/

# run server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]
