FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir ppk2-api==0.9.2 numpy pyserial paho-mqtt

COPY ppk2_logger.py ppk2_fast.py wake_capture.py wake_view.py diag.py live_view.py live_view.html \
     analyse.py auswertung.html ./

# Live-Ansicht (nur wenn LIVE_PORT gesetzt ist)
EXPOSE 8080

CMD ["python", "ppk2_logger.py"]
