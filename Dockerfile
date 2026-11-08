# compression.py 用のイメージ（ghostscript は uv の管理外のため Docker で固定する）
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ghostscript=10.02.1~dfsg1-0ubuntu7.10 \
        python3=3.12.3-0ubuntu2.1 \
    && rm -rf /var/lib/apt/lists/*

COPY compression.py /opt/pdf_edit/compression.py
WORKDIR /data
ENTRYPOINT ["python3", "/opt/pdf_edit/compression.py"]
