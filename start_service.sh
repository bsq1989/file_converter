docker run --name converter_dev -e MINIO_ENDPOINT=172.31.0.2:9000 \
    -e MINIO_ACCESS_KEY=MPA7oqhIweEdD2tNDIFi \
    -e MINIO_SECRET_KEY=tdx0Zx7puyHv7VtljlF33yCLzrHy6GPHsoQwLjyB \
    --network minio_default \
    -p 8016:8000 \
    -idt office_converter:2.1