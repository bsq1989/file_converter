#!/bin/bash
# filepath: /Users/baoshiqiu/code/file_process_for_llm/file_converter/start.sh

# 打印环境变量以便调试
echo "MINIO_ENDPOINT: $MINIO_ENDPOINT"
echo "MINIO_ACCESS_KEY: $MINIO_ACCESS_KEY"
# 其他环境变量...

# 启动应用
uvicorn converter:app --host 0.0.0.0 --port 8000