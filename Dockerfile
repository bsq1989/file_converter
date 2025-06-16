# 使用Python 3.9作为基础镜像
FROM linuxserver/libreoffice:7.6.7

# install pip under Alpine Linux
RUN apk add --no-cache py3-pip
# 设置工作目录
WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
# 复制依赖文件并安装Python依赖
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY converter.py .
COPY ./static/ ./static/
COPY start_converter.sh .
RUN chmod +x start_converter.sh
# 暴露端口
EXPOSE 8000

# 使用脚本启动
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["./start_converter.sh"]
