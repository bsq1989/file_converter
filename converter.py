import os
import uuid
import subprocess
import asyncio
import shutil
import time
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Tuple, Optional, List
from enum import Enum
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel
# 添加MinIO客户端库
from minio import Minio
from minio.error import S3Error
from datetime import timedelta

# 配置项
UPLOAD_DIR = "uploads"
CONVERTED_DIR = "converted"
LIBRE_OFFICE_PATH = os.environ.get("LIBRE_OFFICE_PATH", "soffice")  # 根据实际安装调整
MAX_LIBREOFFICE_PROCESSES = os.environ.get("MAX_LIBREOFFICE_PROCESSES", 3)  # 最大并发LibreOffice进程数
KEEP_LOCAL_FILES = False  # 是否保留本地文件
LOCAL_FILE_TTL = 24 * 60 * 60  # 本地文件保留时间（秒）

# 日志配置
LOG_DIR = "logs"
LOG_FILENAME = "converter.log"
LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVEL = logging.INFO

# 确保日志目录存在
os.makedirs(LOG_DIR, exist_ok=True)

# 配置日志系统
def setup_logger():
    """配置并返回应用logger"""
    logger = logging.getLogger("file-converter")
    logger.setLevel(LOG_LEVEL)
    
    # 如果logger已经配置了handler，就不重复添加
    if logger.handlers:
        return logger
        
    # 创建格式化器
    formatter = logging.Formatter(LOG_FORMAT)
    
    # 文件处理器 - 启用轮转
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILENAME),
        maxBytes=LOG_MAX_SIZE,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(formatter)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(formatter)
    
    # 添加到logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# 初始化日志
logger = setup_logger()

# MinIO配置
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT","localhost:9000")  # MinIO服务器地址
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY","minio")    # 访问密钥
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY","minio")    # 秘密密钥
MINIO_BUCKET = "converted-files"   # 存储桶名称
MINIO_SECURE = False               # 是否使用HTTPS

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CONVERTED_DIR, exist_ok=True)

# 初始化进程池
executor = ProcessPoolExecutor(max_workers=MAX_LIBREOFFICE_PROCESSES)

# 初始化MinIO客户端
minio_client = None
try:
    minio_client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE
    )
    
    # 确保存储桶存在
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
    logger.info("MinIO连接成功")
except Exception as e:
    logger.error(f"MinIO初始化错误: {e}")
    # 继续运行，但文件共享功能将不可用

# 定期清理过期文件的后台任务和任务取消事件
cleanup_task = None
cleanup_stop_event = asyncio.Event()

# 定期清理过期文件
async def periodic_cleanup():
    """定期清理过期的本地文件"""
    while not cleanup_stop_event.is_set():
        try:
            now = time.time()
            expired_tasks = []
            
            # 查找已完成且文件已上传到MinIO的过期任务
            for task_id, task in conversion_tasks.items():
                if (task["status"] == "completed" and 
                    "minio_url" in task and 
                    task.get("created_at", now) + LOCAL_FILE_TTL < now):
                    expired_tasks.append(task_id)
            
            # 清理过期任务的本地文件
            for task_id in expired_tasks:
                cleanup_local_files(task_id)
            
            if expired_tasks:
                logger.info(f"已清理 {len(expired_tasks)} 个过期任务文件")
                
            # 检查uploads和converted目录中的孤儿文件（超过TTL但不在任务记录中的文件）
            # 这部分可以根据需要实现
                
        except Exception as e:
            logger.error(f"定期清理任务出错: {e}")
            
        # 每小时运行一次，但可以被取消
        try:
            # 使用wait_for可以在等待过程中响应取消信号
            await asyncio.wait_for(cleanup_stop_event.wait(), timeout=60*60)
        except asyncio.TimeoutError:
            # 超时意味着继续执行循环
            pass

# 使用上下文管理器管理FastAPI应用的生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    global cleanup_task
    # 创建清理任务
    cleanup_stop_event.clear()
    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("后台清理任务已启动")
    
    # 让控制权回到FastAPI
    yield
    
    # 关闭时执行
    # 停止清理任务
    cleanup_stop_event.set()
    if cleanup_task and not cleanup_task.done():
        try:
            # 等待任务结束，但最多等待5秒
            await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=5.0)
        except asyncio.TimeoutError:
            # 如果任务在5秒内未结束，则取消它
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                logger.info("后台清理任务已取消")
    logger.info("后台清理任务已停止")

# 初始化FastAPI应用，使用lifespan上下文管理器
app = FastAPI(title="Office文档格式转换服务", lifespan=lifespan)
# Configure CORS to allow requests from all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)
# 添加静态文件目录来服务 Swagger UI 资源
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

# 自定义 Swagger UI 端点
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        swagger_js_url="/static/swagger_ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger_ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger_ui/favicon.png",
    )

# 自定义 OpenAPI JSON 端点
@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_endpoint():
    return get_openapi(title="File Proxy API", version="1.0.0", routes=app.routes)

# 支持的转换类型
class ConversionType(str, Enum):
    DOC_TO_DOCX = "doc-to-docx"
    XLS_TO_XLSX = "xls-to-xlsx"
    PPT_TO_PPTX = "ppt-to-pptx"

# 任务状态跟踪
conversion_tasks: Dict[str, Dict] = {}

# 上传文件到MinIO
def upload_to_minio(file_path: str, object_name: str) -> Optional[str]:
    """
    上传文件到MinIO
    
    Args:
        file_path: 本地文件路径
        object_name: MinIO中的对象名称
        
    Returns:
        文件访问URL或None（如果上传失败）
    """
    if minio_client is None:
        return None
        
    try:
        minio_client.fput_object(
            MINIO_BUCKET, object_name, file_path,
        )
        # 返回文件Key ，
        logger.info(f"文件已上传到MinIO: {object_name}")
        return f'{object_name}'
    except Exception as e:
        logger.error(f"上传文件到MinIO失败: {e}")
        return None

# LibreOffice转换函数 (在进程池中执行)
def convert_document(input_file: str, output_dir: str, target_format: str) -> Tuple[bool, Optional[str]]:
    """
    使用LibreOffice转换文档
    
    Args:
        input_file: 输入文件路径
        output_dir: 输出目录
        target_format: 目标格式
        
    Returns:
        (成功标志, 错误信息)
    """
    try:
        # 为每个进程创建唯一的用户配置目录，避免干扰
        user_profile_dir = f"/tmp/libreoffice_userprofile_{os.getpid()}"
        os.makedirs(user_profile_dir, exist_ok=True)
        
        cmd = [
            LIBRE_OFFICE_PATH,
            f"-env:UserInstallation=file://{user_profile_dir}",  # 指定独立用户配置
            "--headless",                                       # 无界面模式
            "--nofirststartwizard",                             # 禁用首次启动向导
            "--convert-to", target_format,
            "--outdir", output_dir,
            input_file
        ]
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        
        # 清理临时用户配置目录
        # subprocess.run(["rm", "-rf", user_profile_dir])
        
        if process.returncode != 0:
            return False, stderr.decode()
        
        # 获取转换后的文件路径
        input_name = Path(input_file).stem
        converted_file = os.path.join(output_dir, f"{input_name}.{target_format}")
        
        # 检查文件是否存在
        if not os.path.exists(converted_file):
            return False, "转换成功但无法找到输出文件"
            
        return True, converted_file
    except Exception as e:
        return False, str(e)

# 删除本地文件
def cleanup_local_files(task_id: str):
    """删除任务相关的本地文件"""
    if KEEP_LOCAL_FILES:
        return
    
    try:
        # 删除上传的原始文件
        task = conversion_tasks[task_id]
        if "file_path" in task and os.path.exists(task["file_path"]):
            os.remove(task["file_path"])
            
        # 删除转换后的文件目录
        task_dir = os.path.join(CONVERTED_DIR, task_id)
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir)
            
        logger.info(f"已清理任务 {task_id} 的本地文件")
    except Exception as e:
        logger.error(f"清理文件失败: {e}")

# 处理转换结果
async def process_conversion_result(task_id: str, future):
    """处理异步转换任务的结果"""
    try:
        success, result = future.result()
        
        if success:
            # 更新任务状态为完成
            conversion_tasks[task_id]["status"] = "completed"
            conversion_tasks[task_id]["converted_file"] = result
            
            # 获取原始文件名和新扩展名
            original_filename = conversion_tasks[task_id]["original_filename"]
            name_without_ext = os.path.splitext(original_filename)[0]
            new_ext = os.path.splitext(result)[1]
            new_filename = f"{name_without_ext}{new_ext}"
            
            logger.info(f"文件转换成功: {task_id}, {new_filename}")
            
            # 上传到MinIO
            object_name = f"{task_id}/{new_filename}"
            minio_url = upload_to_minio(result, object_name)
            if minio_url:
                conversion_tasks[task_id]["minio_url"] = minio_url
                conversion_tasks[task_id]["minio_object"] = object_name
                conversion_tasks[task_id]['bucket'] = MINIO_BUCKET
                
                # 如果不需要保留本地文件且已成功上传到MinIO，则删除本地文件
                if not KEEP_LOCAL_FILES:
                    cleanup_local_files(task_id)
        else:
            # 更新任务状态为失败
            conversion_tasks[task_id]["status"] = "failed"
            conversion_tasks[task_id]["error"] = result
            logger.error(f"文件转换失败: {task_id}, 原因: {result}")
    except Exception as e:
        # 处理异常
        conversion_tasks[task_id]["status"] = "failed"
        conversion_tasks[task_id]["error"] = str(e)
        logger.exception(f"处理转换结果时出错: {task_id}")

# 文件上传与转换
@app.post("/convert")
async def convert_file(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    keep_local: bool = None  # 可选参数，是否保留本地文件
):
    """上传并转换Office文档"""
    # 验证文件扩展名
    filename = file.filename
    file_ext = os.path.splitext(filename)[1].lower()
    
    if file_ext not in [".doc", ".xls", ".ppt"]:
        raise HTTPException(status_code=400, detail="不支持的文件类型。请上传.doc、.xls或.ppt文件")
    
    # 确定目标格式
    target_format = ""
    if file_ext == ".doc":
        target_format = "docx"
    elif file_ext == ".xls":
        target_format = "xlsx"
    elif file_ext == ".ppt":
        target_format = "pptx"
    
    # 保存上传文件
    task_id = str(uuid.uuid4())
    file_id = f"{task_id}{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, file_id)
    
    task_output_dir = os.path.join(CONVERTED_DIR, task_id)
    os.makedirs(task_output_dir, exist_ok=True)
    
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    # 设置任务状态
    conversion_tasks[task_id] = {
        "status": "processing",
        "original_filename": filename,
        "file_path": file_path,
        "created_at": time.time(),  # 记录创建时间
        "keep_local": keep_local if keep_local is not None else KEEP_LOCAL_FILES
    }
    
    # 提交到进程池
    future = executor.submit(
        convert_document,
        file_path,
        task_output_dir,
        target_format
    )
    
    # 添加回调处理结果
    background_tasks.add_task(process_conversion_result, task_id, future)
    
    return {"task_id": task_id, "status": "processing"}

# 获取任务状态
@app.get("/status/{task_id}")
async def get_status(task_id: str):
    """获取转换任务状态"""
    if task_id not in conversion_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return conversion_tasks[task_id]

# 下载转换后的文件
@app.get("/download/{task_id}")
async def download_file(task_id: str):
    """下载已转换的文件"""
    if task_id not in conversion_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task = conversion_tasks[task_id]
    
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"任务尚未完成，当前状态: {task['status']}")
    
    # 如果有MinIO URL，返回URL而不是直接下载
    if "minio_url" in task:
        return {
            "url": task["minio_url"], 
            "message": "使用此URL直接访问文件",
            "usage": "可直接在浏览器中访问，或使用 curl -o 文件名.扩展名 \"URL\" 命令下载"
        }
    
    converted_file = task["converted_file"]
    original_filename = task["original_filename"]
    
    # 获取新的文件名 (保留原始文件名但更改扩展名)
    name_without_ext = os.path.splitext(original_filename)[0]
    new_ext = os.path.splitext(converted_file)[1]
    download_filename = f"{name_without_ext}{new_ext}"
    
    # 如果本地文件已被删除但有MinIO链接，返回MinIO URL
    if not os.path.exists(converted_file) and "minio_url" in task:
        return {
            "url": task["minio_url"], 
            "message": "本地文件已清理，请使用MinIO链接访问",
            "usage": "可直接在浏览器中访问，或使用 curl -o 文件名.扩展名 \"URL\" 命令下载"
        }
    
    return FileResponse(
        path=converted_file,
        filename=download_filename,
        media_type="application/octet-stream"
    )

# 获取MinIO共享链接
@app.get("/share/{task_id}")
async def get_share_link(task_id: str):
    """获取文件的MinIO共享链接"""
    if task_id not in conversion_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task = conversion_tasks[task_id]
    
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"任务尚未完成，当前状态: {task['status']}")
    
    if "minio_url" not in task:
        raise HTTPException(status_code=400, detail="该文件没有可用的共享链接，请使用下载API")
    
    return {
        "url": task["minio_url"], 
        "expires": "24小时",
        "download_command": f"curl -o 下载文件名.扩展名 \"{task['minio_url']}\"",
        "wget_command": f"wget -O 下载文件名.扩展名 \"{task['minio_url']}\"" 
    }

# 健康检查
@app.get("/health")
async def health_check():
    """服务健康检查"""
    return {
        "status": "ok", 
        "service": "file-converter",
        "minio_available": minio_client is not None
    }