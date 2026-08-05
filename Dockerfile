FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 wickless
WORKDIR /app

COPY wickless_bot.py live_scan.py time_display.py no_wick_research.py live_data.py run_daemon.py ./

RUN mkdir -p /app/.runtime-data /app/.signal-state \
    && chown -R wickless:wickless /app

USER wickless

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import live_data, live_scan, time_display, wickless_bot; assert wickless_bot.TIMEFRAME_MINUTES == 15"

CMD ["python", "run_daemon.py"]
