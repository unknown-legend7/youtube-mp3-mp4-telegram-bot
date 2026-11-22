
FROM python:3.12-slim

WORKDIR /app
ARG INSTALL_NATIVE_DOCUMENT_TOOLS=0

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core && \
    if [ "$INSTALL_NATIVE_DOCUMENT_TOOLS" = 1 ]; then apt-get install -y --no-install-recommends bubblewrap libreoffice-writer libreoffice-calc libreoffice-impress ghostscript default-jre-headless; fi && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY verapdf-install.xml /tmp/verapdf-install.xml
RUN set -eu; \
    if [ "$INSTALL_NATIVE_DOCUMENT_TOOLS" = 1 ]; then \
        python -c 'import urllib.request; urllib.request.urlretrieve("https://software.verapdf.org/releases/1.30/verapdf-greenfield-1.30.4-installer.zip", "/tmp/verapdf-installer.zip")'; \
        echo '04415ef3f149526a3048b9889723e81320ddf013bd891bcc7f0f297f3eadcc23  /tmp/verapdf-installer.zip' | sha256sum -c -; \
        python -m zipfile -e /tmp/verapdf-installer.zip /tmp/verapdf-setup; \
        java -jar /tmp/verapdf-setup/verapdf-greenfield-1.30.4/verapdf-izpack-installer-1.30.4.jar /tmp/verapdf-install.xml; \
        test -x /opt/verapdf/verapdf; \
        rm -rf /tmp/verapdf-installer.zip /tmp/verapdf-setup; \
    fi; \
    rm /tmp/verapdf-install.xml

COPY main.py image_routes.py image_worker.py document_routes.py document_worker.py document_native.py pdf_tools.py pdf_office.py native_relay.py ./

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
