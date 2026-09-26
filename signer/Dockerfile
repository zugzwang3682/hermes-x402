# Holds the x402 wallet keys and enforces the spending limits. Only the Hermes container can reach it,
# over the private compose network; it publishes no ports.
FROM python:3.12-slim
RUN useradd --uid 10402 --create-home signer && mkdir /data && chown signer /data && chmod 700 /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY x402signer ./x402signer
USER signer
ENV X402_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8402
ENTRYPOINT ["python", "-m", "x402signer"]
CMD ["serve"]
