ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY tools ./tools
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
