from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import typing

import piexif
import requests
from PIL import Image
from py7zr import SevenZipFile
from PyPDF2 import PdfReader, PdfWriter
from PySide6.QtCore import SignalInstance
from retrying import retry

from src.utils import (
    MAX_RETRY_LARGE,
    MAX_RETRY_SMALL,
    RETRY_WAIT_EX,
    TIMEOUT_LARGE,
    TIMEOUT_SMALL,
    __app_name__,
    __copyright__,
    __version__,
    logger,
)

if typing.TYPE_CHECKING:
    from ui.MainGUI import MainGUI


class Episode:
    """漫画章节类，用于管理漫画章节的详细信息"""

    def __init__(
        self,
        episode: dict,
        sessData: str,
        comic_id: str,
        comic_info: dict,
        mainGUI: MainGUI,
    ) -> None:
        self.mainGUI = mainGUI
        self.id = episode["id"]
        self.available = not episode["is_locked"]
        self.ord = episode["ord"]
        self.comic_name = comic_info["title"]
        self.size = episode["size"]
        self.imgs_token = None
        self.author = comic_info["author_name"]

        # ?###########################################################
        # ? 修复标题中的特殊字符
        episode["short_title"] = re.sub(r'[\\/:*?"<>|]', " ", episode["short_title"])
        episode["short_title"] = re.sub(r"\s+$", "", episode["short_title"])
        episode["short_title"] = re.sub(r"\.", "·", episode["short_title"])
        episode["title"] = re.sub(r'[\\/:*?"<>|]', " ", episode["title"])
        episode["title"] = re.sub(r"\s+$", "", episode["title"])
        episode["title"] = re.sub(r"\.", "·", episode["title"])

        # ?###########################################################
        # ? 修复重复标题
        if episode["short_title"] == episode["title"] or episode["title"] == "":
            self.title = episode["short_title"]
        else:
            self.title = f"{episode['short_title']} {episode['title']}"
        temp = re.search(r"^(\d+)\s+第(\d+)话", self.title)
        if temp and temp[1] == temp[2]:
            self.title = re.sub(r"^\d+\s+(第\d+话)", r"\1", self.title)
        if re.search(r"^特别篇\s+特别篇", self.title):
            self.title = re.sub(r"^特别篇\s+特别篇", r"特别篇", self.title)

        # ?###########################################################
        # ? 修复短标题中的数字
        if re.search(r"^[0-9\-]+话", self.title):
            self.title = re.sub(r"^([0-9\-]+)", r"第\1", self.title)
        elif re.search(r"^[0-9\-]+", self.title):
            self.title = re.sub(r"^([0-9\-]+)", r"第\1话", self.title)

        self.headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36",
            "origin": "https://manga.bilibili.com",
            "referer": f"https://manga.bilibili.com/detail/mc{comic_id}/{self.id}?from=manga_homepage",
            "cookie": f"SESSDATA={sessData}",
        }
        self.save_path = comic_info["save_path"]
        self.save_method = mainGUI.getConfig("save_method")
        self.epi_path_pdf = os.path.join(self.save_path, f"{self.title}.pdf")
        self.epi_path_folder = os.path.join(self.save_path, f"{self.title}")
        self.epi_path_7z = os.path.join(self.save_path, f"{self.title}.7z")

        if self.save_method == "PDF":
            self.epi_path = self.epi_path_pdf
        elif self.save_method == "文件夹-图片":
            self.epi_path = self.epi_path_folder
        elif self.save_method == "7z压缩包":
            self.epi_path = self.epi_path_7z

    ############################################################
    def init_imgsList(self, mainGUI: MainGUI) -> bool:
        """初始化章节内所有图片的列表和图片的token

        Returns
            bool: 是否初始化成功
        """
        # ?###########################################################
        # ? 获取图片列表
        GetImageIndexURL = "https://manga.bilibili.com/twirp/comic.v1.Comic/GetImageIndex?device=pc&platform=web"

        @retry(
            stop_max_delay=MAX_RETRY_SMALL, wait_exponential_multiplier=RETRY_WAIT_EX
        )
        def _() -> list[dict]:
            try:
                res = requests.post(
                    GetImageIndexURL,
                    data={"ep_id": self.id},
                    headers=self.headers,
                    timeout=TIMEOUT_SMALL,
                )
            except requests.RequestException as e:
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title}，获取图片列表失败! 重试中...\n{e}"
                )
                raise e
            if res.status_code != 200:
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title} 获取图片列表失败! 状态码：{res.status_code}, 理由: {res.reason} 重试中..."
                )
                raise requests.HTTPError()
            return res.json()["data"]["images"]

        try:
            imgs_urls = [img["path"] for img in _()]
        except requests.RequestException as e:
            logger.error(f"《{self.comic_name}》章节：{self.title} 重复获取图片列表多次后失败!，跳过!\n{e}")
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 重复获取图片列表多次后失败!\n已暂时跳过此章节!\n请检查网络连接或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )
            return False

        # ?###########################################################
        # ? 获取图片token
        ImageTokenURL = "https://manga.bilibili.com/twirp/comic.v1.Comic/ImageToken?device=pc&platform=web"

        @retry(
            stop_max_delay=MAX_RETRY_SMALL, wait_exponential_multiplier=RETRY_WAIT_EX
        )
        def _() -> list[dict]:
            try:
                res = requests.post(
                    ImageTokenURL,
                    data={"urls": json.dumps(imgs_urls)},
                    headers=self.headers,
                    timeout=TIMEOUT_SMALL,
                )
            except requests.RequestException as e:
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title}，获取图片token失败! 重试中...\n{e}"
                )
                raise e
            if res.status_code != 200:
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title} 获取图片token失败! 状态码：{res.status_code}, 理由: {res.reason} 重试中..."
                )
                raise requests.HTTPError()
            return res.json()["data"]

        try:
            self.imgs_token = _()
        except requests.RequestException as e:
            logger.error(
                f"《{self.comic_name}》章节：{self.title} 重复获取图片token多次后失败，跳过!\n{e}"
            )
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 重复获取图片token多次后失败!\n已暂时跳过此章节!\n请检查网络连接或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )
            return False

        return True

    ############################################################
    def download(
        self, mainGUI: MainGUI, rate_progress: SignalInstance, taskID: str
    ) -> None:
        """下载章节内所有图片 并合并为PDF

        Args:
            mainGUI (MainGUI): 主窗口类实例
            rate_progress (SignalInstance): 信号槽，用于更新下载进度条
            taskID (str): 任务ID
        """

        # ?###########################################################
        # ? 初始化下载图片需要的参数
        if not self.init_imgsList(mainGUI):
            rate_progress.emit(
                {
                    "taskID": taskID,
                    "rate": -1,
                }
            )

        # ?###########################################################
        # ? 下载所有图片
        imgs_path = []
        for index, img in enumerate(self.imgs_token, start=1):
            img_url = f"{img['url']}?token={img['token']}"

            img_path = self.downloadImg(mainGUI, index, img_url)
            if img_path is None:
                rate_progress.emit(
                    {
                        "taskID": taskID,
                        "rate": -1,
                    }
                )
                self.clearAfterSave(mainGUI, imgs_path)
                return

            imgs_path.append(img_path)
            rate_progress.emit(
                {
                    "taskID": taskID,
                    "rate": int((index / len(self.imgs_token)) * 100),
                    "path": self.epi_path,
                }
            )

        # ?###########################################################
        # ? 统一转换为RGB模式
        temp_imgs = [Image.open(x) for x in imgs_path]
        for i, img in enumerate(temp_imgs):
            if img.mode != "RGB":
                temp_imgs[i] = img.convert("RGB")

        # ?###########################################################
        # ? 保存图片

        # 现版本的 pyinstaller 不支持switch语句，等待后续更新
        # match self.save_method:
        #     case 'PDF':
        #         self.saveToPDF(mainGUI, temp_imgs)
        #     case '文件夹-图片':
        #         self.saveToFolder(mainGUI, temp_imgs)
        #     case '7z压缩包':
        #         self.saveTo7z(mainGUI, temp_imgs)

        if self.save_method == "PDF":
            self.saveToPDF(mainGUI, temp_imgs)
        elif self.save_method == "文件夹-图片":
            self.saveToFolder(mainGUI, temp_imgs)
        elif self.save_method == "7z压缩包":
            self.saveTo7z(mainGUI, temp_imgs)

        self.clearAfterSave(mainGUI, imgs_path)

    ############################################################
    def clearAfterSave(self, mainGUI: MainGUI, imgs_path: list[str]) -> None:
        """删除临时图片, 偶尔会出现删除失败的情况，故给与重试5次

        Args:
            mainGUI (MainGUI): 主窗口类实例
            imgs_path (list): 临时图片路径列表
        """

        @retry(stop_max_attempt_number=5)
        def _() -> None:
            for img in reversed(imgs_path):
                try:
                    os.remove(img)
                    if os.path.exists(img):
                        raise OSError()
                except OSError as e:
                    logger.warning(
                        f"《{self.comic_name}》章节：{self.title} - {img} 删除临时图片失败! 重试中..."
                    )
                    raise e
                imgs_path.remove(img)

        try:
            _()
        except OSError as e:
            logger.error(
                f"《{self.comic_name}》章节：{self.title} 删除临时图片多次后失败!\n{imgs_path}\n{e}"
            )
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 删除临时图片多次后失败!\n请手动删除!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )

    ############################################################
    def saveToPDF(self, mainGUI: MainGUI, temp_imgs: list[Image.Image]) -> None:
        """将图片保存为PDF文件

        Args:
            mainGUI (MainGUI): 主窗口类实例
            temp_imgs (list): 临时图片路径
        """

        @retry(stop_max_attempt_number=5)
        def _():
            try:
                temp_imgs[0].save(
                    self.epi_path_pdf,
                    save_all=True,
                    append_images=temp_imgs[1:],
                    quality=95,
                )
                # 在pdf文件属性中记录章节标题作者和软件版本以及版权信息
                with open(self.epi_path_pdf, "rb") as f:
                    pdf = PdfReader(f)
                    pdf_writer = PdfWriter()
                    pdf_writer.append_pages_from_reader(pdf)
                    pdf_writer.add_metadata(
                        {
                            "/Title": f"《{self.comic_name}》 - {self.title}",
                            "/Author": self.author,
                            "/Creator": f"{__app_name__} {__version__} {__copyright__}",
                        }
                    )
                    with open(self.epi_path_pdf, "wb") as f:
                        pdf_writer.write(f)

            except OSError as e:
                logger.error(f"《{self.comic_name}》章节：{self.title} 合并PDF失败! 重试中...\n{e}")
                raise e

        try:
            _()
        except OSError as e:
            logger.error(f"《{self.comic_name}》章节：{self.title} 合并PDF多次后失败!\n{e}")
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 合并PDF多次后失败!\n已暂时跳过此章节!\n请重新尝试或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )

    ############################################################
    def saveToFolder(self, mainGUI: MainGUI, temp_imgs: list[Image.Image]) -> None:
        """将图片保存到文件夹

        Args:
            mainGUI (MainGUI): 主窗口类实例
            temp_imgs (list): 临时图片路径
        """

        @retry(stop_max_attempt_number=5)
        def _():
            try:
                for index, img in enumerate(temp_imgs, start=1):
                    # 在图片文件属性中记录章节标题作者和软件版本以及版权信息
                    info = {}
                    info[
                        piexif.ImageIFD.ImageDescription
                    ] = f"《{self.comic_name}》 - {self.title}".encode("utf-8")
                    info[piexif.ImageIFD.Artist] = self.author.encode("utf-8")
                    info[
                        piexif.ImageIFD.Software
                    ] = f"{__app_name__} {__version__}".encode("utf-8")
                    info[piexif.ImageIFD.Copyright] = __copyright__.encode("utf-8")
                    exif_bytes = piexif.dump({"0th": info})
                    img.save(
                        os.path.join(
                            self.epi_path_folder, f"{str(index).zfill(3)}.jpg"
                        ),
                        exif=exif_bytes,
                    )

            except OSError as e:
                logger.error(
                    f"《{self.comic_name}》章节：{self.title} 保存图片到文件夹失败! 重试中...\n{e}"
                )
                raise e

        try:
            if not os.path.exists(self.epi_path_folder):
                os.makedirs(self.epi_path_folder)
            _()
        except OSError as e:
            logger.error(f"《{self.comic_name}》章节：{self.title} 保存图片到文件夹多次后失败!\n{e}")
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 保存图片到文件夹多次后失败!\n已暂时跳过此章节!\n请重新尝试或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )

    ############################################################
    def saveTo7z(self, mainGUI: MainGUI, temp_imgs: list[Image.Image]) -> None:
        """将图片保存到7z压缩文件

        Args:
            mainGUI (MainGUI): 主窗口类实例
            temp_imgs (list): 临时图片路径
        """

        self.saveToFolder(mainGUI, temp_imgs)

        @retry(stop_max_attempt_number=5)
        def _():
            try:
                with SevenZipFile(f"{self.epi_path_7z}", "w") as z:
                    # 压缩文件里不要子目录，全部存在根目录
                    for root, _dirs, files in os.walk(self.epi_path_folder):
                        for file in files:
                            z.write(
                                os.path.join(root, file),
                                os.path.basename(os.path.join(root, file)),
                            )
                    shutil.rmtree(self.epi_path_folder)
            except OSError as e:
                logger.error(
                    f"《{self.comic_name}》章节：{self.title} 保存图片到7z失败! 重试中...\n{e}"
                )
                raise e

        try:
            _()
        except OSError as e:
            logger.error(f"《{self.comic_name}》章节：{self.title} 保存图片到7z多次后失败!\n{e}")
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 保存图片到7z多次后失败!\n已暂时跳过此章节!\n请重新尝试或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )

    ############################################################
    def downloadImg(self, mainGUI: MainGUI, index: int, img_url: str) -> str:
        """根据 url 和 token 下载图片

        Args:
            mainGUI (MainGUI): 主窗口类实例
            index (int): 章节中图片的序号
            img_url (str): 图片的合法 url

        Returns:
            str: 图片的保存路径
        """

        # ?###########################################################
        # ? 下载图片
        @retry(
            stop_max_delay=MAX_RETRY_LARGE, wait_exponential_multiplier=RETRY_WAIT_EX
        )
        def _() -> bytes:
            try:
                res = requests.get(img_url, timeout=TIMEOUT_LARGE)
            except requests.RequestException as e:
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title} - {index} - {img_url} 下载图片失败! 重试中...\n{e}"
                )
                raise e
            if res.status_code != 200:
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title} - {index} - {img_url} 获取图片 header 失败! 状态码：{res.status_code}, 理由: {res.reason} 重试中..."
                )
                raise requests.HTTPError()
            if res.headers["Etag"] != hashlib.md5(res.content).hexdigest():
                logger.warning(
                    f"《{self.comic_name}》章节：{self.title} - {index} - {img_url} - 下载内容Checksum不正确! 重试中...\n\t{res.headers['Etag']} ≠ {hashlib.md5(res.content).hexdigest()}"
                )
                raise requests.HTTPError()
            return res.content

        try:
            img = _()
        except requests.RequestException as e:
            logger.error(
                f"《{self.comic_name}》章节：{self.title} - {index} - {img_url} 重复下载图片多次后失败!\n{e}"
            )
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} 重复下载图片多次后失败!\n已暂时跳过此章节!\n请检查网络连接或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )
            return None

        # ?###########################################################
        # ? 保存图片
        path_to_save = os.path.join(self.save_path, f"{self.ord}_{index}.jpg")

        @retry(stop_max_attempt_number=5)
        def _() -> None:
            try:
                with open(path_to_save, "wb") as f:
                    f.write(img)
            except OSError as e:
                logger.error(
                    f"《{self.comic_name}》章节：{self.title} - {index} - {img_url} - {path_to_save} - 保存图片失败! 重试中...\n{e}"
                )
                raise e

        try:
            _()
        except OSError as e:
            logger.error(
                f"《{self.comic_name}》章节：{self.title} - {index} - {img_url} - {path_to_save} - 保存图片多次后失败!\n{e}"
            )
            logger.exception(e)
            mainGUI.message_box.emit(
                f"《{self.comic_name}》章节：{self.title} - {index} - 保存图片多次后失败!\n已暂时跳过此章节, 并删除所有缓存文件！\n请重新尝试或者重启软件!\n\n更多详细信息请查看日志文件, 或联系开发者！"
            )
            return None

        return path_to_save

    ############################################################
    def isAvailable(self) -> bool:
        """判断章节是否可用

        Returns:
            bool: True: 已解锁章节; False: 需付费章节
        """

        return self.available

    ############################################################
    def isDownloaded(self) -> bool:
        """判断章节是否已下载

        Returns:
            bool: True: 已下载; False: 未下载
        """
        return (
            os.path.exists(self.epi_path_pdf)
            or os.path.exists(self.epi_path_folder)
            or os.path.exists(self.epi_path_7z)
        )
