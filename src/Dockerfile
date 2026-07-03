FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV FILAMENT_OBJECT=filament_detect
ENV ASSIGN_GCODE_TEMPLATE="SET_TOOL_SPOOL TOOL={tool} ID={spool_id}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY u1_spoolman_bridge.py .

RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app

USER app

CMD ["python", "-u", "u1_spoolman_bridge.py"]
