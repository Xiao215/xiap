FROM --platform=linux/amd64 python:3.10 AS build

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python3", "app.py"]