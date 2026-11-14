FROM python:3.12-alpine

LABEL maintainer="teleagent-signin"
WORKDIR /app

COPY teleagent_webui.py /app/

# 零第三方依赖，标准库即可；数据目录由 volume 挂载
RUN mkdir -p /app/data

EXPOSE 8002

ENV WEBUI_PASSWORD=admin123 \
    TELE_PORT=8002 \
    TELE_SIGN_HOUR=0

CMD ["python3", "teleagent_webui.py"]
