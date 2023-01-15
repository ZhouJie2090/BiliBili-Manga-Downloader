import datetime
import hashlib
import json
import math
import os
import re
import textwrap
import threading
import time
from os import mkdir, path, remove
from PIL import Image

import img2pdf
import requests
from rich import print
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

console = Console()

def timeStr():
    t = datetime.datetime.fromtimestamp(time.time())
    timeStr = t.strftime("[ %Y.%m.%d %H:%M:%S ]")
    return f"{timeStr}"


def info(msg):
    logStr = f"{timeStr()} [b]|[rgb(51, 204, 204)]INFO[/]|[/b] {msg}"
    print(logStr)


def error(msg):
    logStr = f"{timeStr()} [b]|[rgb(204, 0, 0)]ERROR[/]|[/b] {msg}"
    print(logStr)


def splitThreads(data, num):
    for i in range(math.ceil((len(data) / num))):
        start = i * num
        end = min((i + 1) * num, len(data))
        yield data[start:end]
        
def requireInt(msg, notNull):
    while True:
        userInput = input(msg)
        try:
            return None if len(userInput) == 0 and (not notNull) else int(userInput)
        except ValueError:
            error('请输入数字...')

class Comic:
    def __init__(self, comicID: int, sessdata: str, rootPath: str) -> None:
        self.comicID = comicID
        self.sessdata = sessdata
        self.rootPath = rootPath
        info(f'初始化漫画 ID {comicID}')
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36 Edg/90.0.818.56',
            'origin': 'https://manga.bilibili.com',
            'referer': f'https://manga.bilibili.com/detail/mc{comicID}?from=manga_homepage',
            'cookie': f'SESSDATA={sessdata}'
        }
        self.threads = 16
        self.analyzeData()

    def analyzeData(self) -> None:
        """
        使用哔哩哔哩漫画 API 分析漫画数据。
        Analyze data of a comic using the Bilibili Manga API.
        """
        
        # 爬取漫画信息
        detailUrl = 'https://manga.bilibili.com/twirp/comic.v1.Comic/ComicDetail?device=pc&platform=web'
        payload = {"comic_id": self.comicID}
        with console.status('正在访问 BiliBili Manga'):
            res = requests.post(detailUrl, data=payload, headers=self.headers)
            if not res.ok:
                error('请求错误 / 网络错误!')
                error(f'详细信息: {res.status_code}')
                error("请检查输入信息是否正确!")
                exit(1)

        # 解析漫画信息
        info('已获取漫画信息!')
        info('开始解析...')
        data = res.json()
        if data['code']: error(f'漫画信息有误! 请仔细检查! (提示信息{data["msg"]})')
        self.title = data['data']['title']
        self.authorName = data['data']['author_name']
        self.styles = data['data']['styles']
        self.evaluate = data['data']['evaluate']
        self.total = data['data']['total']
        self.savePath = f"{rootPath}/《{self.title}》 作者：{', '.join(self.authorName)}"

        # 打印漫画信息
        t = Table(title='漫画作品详情')
        t.add_column('[green bold]作品标题[/green bold]')
        t.add_column('[green bold]作者[/green bold]')
        t.add_column('[green bold]标签[/green bold]')
        t.add_column('[green bold]概要[/green bold]')
        t.add_column('[green bold]总章节数[/green bold]')
        t.add_row(self.title, ', '.join(self.authorName), ''.join(self.styles), textwrap.fill(self.evaluate, width=30), str(self.total))
        print(t)

        # 选择下载章节
        while True:
            start = requireInt('开始章节(不输入则不限制): ', False)
            start = 0 if start is None else start
            end = requireInt('结束章节(不输入则不限制): ', False)
            end = 2147483647 if end is None else end
            if start <= end: break
            error('开始章节必须小于结束章节!')
        
        # 解析章节
        self.episodes = []
        with console.status('正在解析详细章节...'):
            epList = data['data']['ep_list']
            epList.reverse()
            for episode in epList:
                epi = Episode(episode, self.sessdata, self.comicID, self.savePath)
                if start <= epi.ord <= end and epi.getAvailable():
                    self.episodes.append(epi)

        # 打印章节信息
        print("已选中章节:")
        for episode in self.episodes:
            print(f"\t{episode.title}")
        info(f'分析结束 将爬取章节数: {len(self.episodes)} 输入回车开始爬取!')
        input()

    def fetch(self) -> None:
        """
        使用多线程获取和下载漫画数据
        Fetch and download comic data using multiple threads.
        """
        # 初始化储存文件夹
        if not path.exists(self.rootPath):
            mkdir(self.rootPath)
        if path.exists(self.savePath) and path.isdir(self.savePath):
            info('存在历史下载 将避免下载相同文件!')
        else:
            mkdir(self.savePath)

        # 多线程爬取
        with Progress() as progress:
            epiTask = progress.add_task(f'正在下载 <{self.title}>', total=len(self.episodes))
            for epis in splitThreads(self.episodes, self.threads):
                threads = []
                for epi in epis:
                    t = threading.Thread(target=epi.download)
                    t.start()
                    threads.append(t)

                for t in threads:
                    t.join()
                    progress.update(epiTask, advance=1)
        info('任务完成!')

class Episode:
    def __init__(self, episode, sessData: str, comicID: str, savePath: str) -> None:
        self.id = episode['id']
        self.available = not episode['is_locked']
        self.ord = episode['ord']
        self.title = re.sub(r'[\\/:*?"<>|]', ' ', episode['short_title'] + ' ' + episode['title'])
        
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36 Edg/90.0.818.56',
            'origin': 'https://manga.bilibili.com',
            'referer': f'https://manga.bilibili.com/detail/mc{comicID}/{self.id}?from=manga_homepage',
            'cookie': f'SESSDATA={sessData}'

        }
        self.savePath = savePath
    
    def getAvailable(self ) -> bool:
        return self.available
    
    def download(self) -> bool:
        # 相同文件名已经存在 跳过下载
        if os.path.exists(f'{self.savePath}/{self.title}.pdf'):
            return False
        
        # 获取图片列表
        GetImageIndexURL = 'https://manga.bilibili.com/twirp/comic.v1.Comic/GetImageIndex?device=pc&platform=web'
        res = requests.post(GetImageIndexURL, data={'ep_id': self.id}, headers=self.headers)
        if res.ok:
            data = res.json()
            images = data['data']['images']
            paths = [img['path'] for img in images]
        else:
            error(f'获取图片列表失败! {res.status_code} {res.reason}')
            exit(1)
        
        # 获取图片token
        ImageTokenURL = "https://manga.bilibili.com/twirp/comic.v1.Comic/ImageToken?device=pc&platform=web"
        res = requests.post(ImageTokenURL, data={"urls": json.dumps(paths)}, headers=self.headers)
        if res.ok:
            imgs = [
                self.downloadImg(index, img['url'], img['token'])
                for index, img in enumerate(res.json()['data'], start=1)
            ]
        else:
            error(f'获取图片token失败! {res.status_code} {res.reason}')
            exit(1)
            
        # 旧方法，偶尔会出现pdf打不开的情况
        # with open(os.path.join(self.savePath, f"{self.title}.pdf"), 'wb') as f:
        #     f.write(img2pdf.convert(imgs))
            

        # 新方法
        tempImgs = [Image.open(x) for x in imgs]
        for i,img in enumerate(tempImgs):
            if img.mode != 'RGB':
                tempImgs[i] = img.convert('RGB')
        tempImgs[0].save(os.path.join(self.savePath, f"{self.title}.pdf"), save_all=True, append_images=tempImgs[1:])
        

        for img in imgs:
            remove(img)
            
        info(f'已下载 <{self.title}>')
        return True

    def downloadImg(self, index: int, url: str, token: str) -> None:
        
        while True:
            url = f"{url}?token={token}"
            file = requests.get(url)
            if file.headers['Etag'] == hashlib.md5(file.content).hexdigest():
                break
            error(f"{self.title} 下载内容Checksum不正确! {file.headers['Etag']} ≠ {hashlib.md5(file.content).hexdigest()}")

        pathToSave = os.path.join(self.savePath, f"{self.ord}_{index}.jpg")
        with open(pathToSave, 'wb') as f:
            f.write(file.content)
        return pathToSave


if __name__ == '__main__':
    rootPath = "C://Users//Zeal//Desktop//漫画"
    
    # comicID = requireInt('请输入漫画ID: ', True)
    # userInput = input('请输入SESSDATA (免费漫画请直接按下enter): ')
    
    comicID = 24442
    # sessdata = '6a2f415f%2C1689285165%2C3ac9d%2A11'
    sessdata = 'f5230c77%2C1689341122%2Cd6518%2A11'
    manga = Comic(comicID, sessdata, rootPath)
    manga.fetch()
