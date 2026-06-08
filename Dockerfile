# Playwright's official image ships Chromium + all system libs preinstalled,
# which Railway's default build does not. Version matches playwright==1.56.0.
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Ensure the chromium build matching our pip playwright is present.
RUN playwright install chromium

COPY . .

# The bot also runs the digest + reminder scheduler in-process.
CMD ["python", "assistant_step4.py"]
