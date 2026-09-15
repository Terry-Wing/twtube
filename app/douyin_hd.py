"""抖音 (Douyin) 最高清视频直连下载器。

为什么需要这个模块
--------------------
yt-dlp 自带的抖音提取器直接调用 ``aweme/v1/web/aweme/detail/`` 接口，但不计算签名，
抖音只会返回带水印的 720p 播放地址，永远不会返回包含 1080P/4K 的 ``video.bit_rate`` 列表。
真正的高清流（1080P/4K 原画）只在这个 ``bit_rate`` 列表里，且只有当请求带上有效的
``a_bogus``（或 ``X-Bogus``）签名 + 新鲜的 ``ttwid``/``msToken`` Cookie 时才会下发。

本模块职责：
1. 解析短链 (v.douyin.com / iesdouyin.com) → 视频 ID
2. 生成新鲜的 ttwid + msToken（或复用用户上传的 cookies.txt）
3. 用 a_bogus（回退 X-Bogus）给 detail 接口签名
4. 从 bit_rate 列表里挑选最高清（分辨率优先、码率次之）的无水印直链
5. 流式落盘下载

签名算法来自 Johnserf-Seed/f2（Apache-2.0），见 douyin_xbogus.py / douyin_abogus.py。
注意：抖音会不定期更换签名算法，届时需要与上游 f2 重新同步这两个文件。
"""

import os
import re
import json
import time
import random
import logging
import asyncio

import aiohttp

from douyin_xbogus import XBogus

try:
    from douyin_abogus import ABogus, BrowserFingerprintGenerator
    _HAS_ABOGUS = True
except ImportError:  # 缺少 gmssl 依赖时优雅降级
    _HAS_ABOGUS = False

log = logging.getLogger('douyin_hd')

DEFAULT_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
)
REFERER = 'https://www.douyin.com/'
DETAIL_BASE = 'https://www.douyin.com/aweme/v1/web/aweme/detail/'

# ---- 抖音 msToken 生成配置（来自 f2）----
MSTOKEN_URL = 'https://mssdk.bytedance.com/web/r/token?ms_appid=6383&msToken=T4bNG9W2rKF7hBNwaYssDErnJEobDAk641DFaOn4hcsfAM8slpbZeKPM4Ml4rhDQq18iY8nQ0JR3J87SLZtDiDqtZdZawfBjCWAgtolQsoEtG6MLETvo4fwr7F28zGJUFDdJgKEZHibNR0QshVBv28ygsQsJDzerKAtsgj9Pn5WsxyS1vfkiX3I%3D'
MSTOKEN_MAGIC = 538969122
MSTOKEN_VERSION = 1
MSTOKEN_DATATYPE = 8
MSTOKEN_ULR = 0
MSTOKEN_STRDATA = 'fW15xyeivmE5JAQZdb83gdUCCHlGDBZDeWxqYklwOYciPisi772aWHSG75OFvFZ5zS5RlfrFGzxNzRQllBoIw2wXT5VvEO9UzRqLMD2kh96/p8aCc56JCdvtz6oZx/j9vRUiy5Hdy4OGKqH7e0VqjP2biY6Zi27XiuWv6ZJ/owedPUULhR2LmyhLRAm6wZA3zRj6z6XiZQU64oWdAorw2Q03RCFp7AF9WPmXdgRDCQl/33NPthRL/TBLdJkEFtRLBmY29phw0WqI6dt6JdKEK+5Sdj7DdJj0ckrqCL0MJcdnyD1Ww5ZSCafBK0xMRhHQ3o39AfD6t0D5O4CtrpULW0+fWG755BnIAZnfmsc2SSxV+KwZKWY61Zx/MNju+S6TOKmDbL5w61ceRyTTCNeDmPxAJdp8qmsZnJrczwKgze71YMq3DeZdfg7cf9/RwqroB8TRilvCcLk63r/FLuGUr2+5Y7fA3KiiYNwhYJFzH/6T4Jo8R7Jy5QcBDa7loP4Q0uqYzP09BskRAwiZcg+iZrdC1aJ06zfUxcUi7Q+EtA2S0Z6kGIanoqEfx+va2rIOBIZEn6+Bv2hGmPMwM0trm96KYCvATPdwdVEowKzuuajJFwic78mD+V3tIHlVWeXDqtNm2bRP+9nY9ZvS/fl7UuCbJLYxIekN77btrzKs/rrzCpoRoHvOuIDeXWBusLiJIU2ooa1AkXHitRoVcX57NJAYxb6G+w8V04B4EphNcBL6Xl/wD7EvIjHf7vIUqbcc70xh2CD+ZmufsFBTTa5bOKoj6SJDay3ni8V98n2ZGXKMSj415Mx3VNe/EuxDKUOCpksLmGW6hoK8K0H6QqiNPCseSZ3Cv3iuF0yILlTEiHWwkbyUwujwqi09ZznmoVyV5M9fdAIZ72EgEdpuTt/kh6DFGJ0Y9UdYih46SncUuYQCazLRTlkXlTAZ7q0/RhAdaR0zZzdhu1yHLJbK/upR9jFUI+5rOpjio6Y29cXGHX3i9lea/K0SocQLGa8jSg1AYG6rlVfhdYbPCQ8X53mmf1C+JOJaZTBnUoKXSev5xxotTeWruWLq3JrKxXQxEOYEsNS+zbUT/C4/Mfwop9IQ1FlRMPMvE0azbZI/Cmh3TIkXQRV6B/Yj8O+dBYINuHPXjyQ8A0648fXCjom3mnbl9Anr4K2h0o9MZ7WHDd+ZPi892QBvt1xZcDCq3v8pe9VqUY6uQoe4ex0xKMoA03ETfw5x9c+ow5/BC1Lpxjp0liCKt/6wJ165jA63FMSLRAkn1n61hrpesBzd5eFpPpN7NB2itqcTPusSFyj2YBdpTxYjFnh++E1vHFQvktJIwqjwY99l0ySSVm8Xs+IjK1DQc5frXnQnJyaxXhFmitDHFoKQiJd/6XZIbC1gt+Hi/4j1LzijCb4kWGf5sFLz7I0eZdQJHquoIZ7hdNz/qlrTEH1UBitF7sRv8PbErg063C8anB2UBQsUKIRfKufgVhmneuSqBVUS2P3XkDFlJ043kZ4awB2F3mp/G1g7xr3RiM/OUKippXiJbB9WSDGaWsCl8er7lSpVWQKndaIS64jJ/vyqQC2EB8prWFtVCyBlTTVm+VVSOeQ3n8x3PYhVhPLAlzhleApNr3PNWZOcPWD16wVQ6s/PXcPzHVomUO5EmUC7L3JrNclYxG4iEtHS+GO8FOIPVfc8W8gTvBvhLl8dLX0OsNjXLOMwKvixcr6kUBwnjo0Nn3b7kX80ew/7xnr64evN1KtYRFNWrfahvjfhvyTcoKrzr7dlzI8QpsPG4MdggWzODaRfQhM/B3Awo6ezWGj5K87eMtAheL6b2hZZdvKGDRVoTBI8ebpYh9oUlPARwkhanW1B86Gpi04UAdJVrJ2S6TWhq+/dX8udhhDuDsxwyc0qfjTdjUzNhbd3HzvrNNhoSaBgOb4sSsseULL5NFBQNcT+0sRfjsgWzF5hKExghKwd74j8l5ke3BqKd1UgM3Geb1VC74FXuBVLOY5RNbqtqD3BncJgB89sqU6bCtOf6kStVSiplrE5eqa2eWHPKyCTc9Y4SKyi8PjlVUqc/NMEm7BTQnvy65+7REafIveeDF9kIORttPXK3UJ/uNtBL3LWra1Mmtt0NrCM1/lD1/IvynTxAlsMfoLCgAICuMSr6DHEzLis5Tesi9/1iAdcpebDVMDD2O5aiPWCKNcShsry2k5btGf0mED1km2CSis/SwTVqpzghuKzIo9s5ihCfH/VTkMA8zGxDDAHJfDiSe3VsTPEtQQ6klqpQAdfYjPk7ZB43vX0VG9pA2seO2CPvEpw+qxO5F8Rg12TzILFT1ovktwh2Ss1l2DmgOPhJV4IJX1//N3tYpAQ01PnDqRXPiz0H7m3FsmYGpz6FqKdigc5js3iy9ppd04kG2tok3RstbfJbiW+ZT+snJP5fZhonA1HMfsb0r/1lPxHwoQC6JGcS8ygyhM6Wao6Olq/BjcMZDgQSS9zQo9zOFyy/jCThSRt32/YqgnufK+kbagt9aSFYkx5hMgKwXSYGApQMEZ7ruP5zVsYRyHszKYnTvYyD0kDtYoqjTupzDW6h2MN61XeGq2folpnzo/O7Nep0squ7A7Cr7KHB4mvmqOztaJUoFlwMIWqL/4ulxs2rJBB35GDwWASLSCnYwB9mQ3+tYu0Bsu4mQN1CiFonwlzjN/M+cRkVJR7YRe7jFxr8q6NYtjWz1rVmkS6ZWl0095sgy7fVh02DMxnaaXxE7lp9goTRxmOFs7M4cBStN5uSBRmA2h0SxeCRotyTQ32CfHeCgUwUZGer5inSyDg3S3+bImLqAYfrw1jqrlBTG2aQqPZcuAlNZJnQTT/GQtmjC6uRgS/1gqYIxMR1QBI8x72C7fO9OTDbphW6vNOJIOtBXvAdcyF4wKZOdfCgfjzEnuKpfpRAX3zUr62R8LvctFF4eiDQgdqqKdiC4Qf+KKPoKE3x5qXF5BUiSufEkNKC1E9IiMWDUNodnqGmflnoo4R7D8AHGpilx2Cwe2P5MiNG/ZCDf1WlSpWip4E7fG0wJXCL0vfgVK7APBveHqw15zq+BTwg+S9NqzcjC4zuNzWFwA5orn7CeSIZwskKR58F4jHShpwCIll7V7PQ/blpqUndMwBMsrK3vdOjn7Q0awAsIwOkQRcBGyemnz8krOxbr4s8FCt3ZCOuK4nPWRE9ANOUJiAU9C71kaQF5gwIWQD6RqKTLMKymdTjFuSVWyuwQovLZ3lPt7fCEoF+wwBra++o2A54ML3U+UYU1TIg8kufB14kMftPJXBL80eCfpy5aCNvUyaSAnW8kx/rcYN2wBMAgESUr0c4xbJG28pn5FStjRlS1sIMvhI8z1ihIovXQCcjTA29gUZRntiFpDD6JP74T5kjZelSOgRePdXcQoEXqu0PwL4jlnHMbqt+i3Zg0OiBnwhQfMlQhP1ImhezKs8rhj7rJpRdwH5mI05Fexen0u3nIhDUyV5PTPCEle/87YZ1DNm94VYeaEwheeNqvLPaFgoBczl3nlO6xw8W4qXrYt+mECZAotgQ93Ye3gie3EwsxMoGDRpOYCWCEWj4dz7mKeXEWBXUS7pjAzIScb+9EC8fQdbvAQIWHG4llv/z6wjpDKxQOhr6hP0xhphJ3wkol/Tg5nItswC5uM/ztBcT3zywTLYFD4RoUe9eHsGvXs/yCcMG+WwXhy7D1IT+QbsUuSkrgWZeS1nHoyoifClLCfFrxuUhlJFbRCsraFJ6cbE3GRal+dFD7GWKfmiv8bpsg2q/vIzUpl8PoUu5bDLdGSWoPvW3EvTff9DjrIfw9TwyUOQLnCthpxWeMU54k6pT5Emx46LKZO9Vf73bccgnIx3lCr/ZcFAE7fHXvK9N0SGHlZw7mzl07Hxfg5QLSDxrNoXBBJpM3SfMLVMzeZ5R1Rpy4NZoLxUbJGU0RiPKKIo2f/3/qIbAxrY2P5CMP6RFe2UyzRe+4z4cXCDcrbXYP4IxrVbAhTUG3+C+/B9RocUeoHt2jnlOHFtx5jfqAXM1osiCMrytMfjd2UdGJ/vCYjYBdz6Hys9YQ3E17FRwwZGvrZF8G4+UuUA7nnDZZN3PITviuPWuRwhXOqGc5A7ce1hbkApaVlo01OQtRU/lsg64t/TxHE5/IYXyrngbhcyoClDElpgc62+eayD3Im+i7y2E+vUjCz1T/Le+Sh9zBBd9NUU/09JhWeIQA4eXGDX9kdZBfdbtK6NkFdNkSme8QyGzR0K5VqA65BD/nzrzQvCXdu/Ulopwc863+yRHBb0qBhJXAqHMlfLAV/ViOjCN/LAl4fdbTp8vg3p7fqu+lpyNvOfTZcJKrk0LiG+N4ttMPDEkVBdKbJjLUQRJGFSPnDhO3cKza5zAkqIYcDIegCq/MCW0ULo8Rd4v3loKg72aiQuGpW+OUmunPkXsBMlJjXWDjZ3gmO63Nq9RXIICF7n7rd/GQLTb86I2qt1W4T87dcaPutfmUX52KQQ0VUhQQvQAp4IRvPsodeFKJG7idO4bj40O+iKMTJbGuYPm03XDEpkTTJJMWF9quL5vRp1TvCNmiQQw9irmjY5pdSIKFI3txU6YlCiq4cqXKmqyMjQpAb6ik4AJFuQ1ipl/3Ih/aLONdzFfa+o51KebLCOw3hNI9J6BAkosr4Dfg35L/COKerr91CgliQPXDh7egj5s9FASDQe5kBLPP4NqXn0IGqgG/yYdfc1i0EDR+ln6cymt3X0uT+Kd5SazPg9UjEmwdOaK9pCItOp5/w9A+an/FhUcO+Wak4AXlgY82ts8Omcg3ARJdwle+0Z7xshHH7dTwI/peXkpj65JZc6KUkyacpTeB6dMhbDVi/kQrdpRlYmoCRRhH9DYlS4TzMfBcjfg+SjJVtFlMn+gXna1eeFTmAKWics87tugTJ7EI3sRZ6NImQZ+h51eINfSaVrnQQbYVP8aSECpSVQjkyezMzwtHC/gToUem4q1ZeYCCxLqGiKtMclD1HXjKZv4UtURWYh4OIQaxMyXUlIVWkpEBmLo4Vbs42efEXv2UGNJ/0WbT5p/thcu/NerBxd2ngtDn5nhhIDg/52psjPOWBG6fxAMAcRGQxtE4LuahftPHuAt9CkeUWWOM05BiNOpFJMQxpLqQlVIuN3/VFNEwzHGcvR2Q938FljJ1u3QamGCmrHyVHTMRE1SeWjtdvCItk98Gxs49y4wnAETtbfHqdS1ZpOjqEg8+MzrJa70a69/16//gDcQ8EXBwzk6U7TBdD+Q251zPDJ7NYeZ3sLiraXn2Pt3bF67W5kFdOFYYmHSWcnkzQ05UClk5HvWwEKEFg8S/hNu6in0jKHy0Q1RzVrF5c/C18RiRgvrOainDVuR0wttwpjdJVoRW18+3nxjMdfV4eJLd8Z477NMrov62YJiKdKXEKkJg2C/yz6baQpwX403RjroJMXwVorGgp6GN/uXoMWQockCl1kFGfIHTWpEpcEcqeyQs8rEdnQ/wuR+LJYoBlIScj9L5SPA02ItVW7eKUWGKuzrZIjVp17URzgcFw46u1Ap8FO4drpeOgvfb1SGchdGPcFu6xTxtKddYd0ycbnOQRAvD0seX7sBuWL/XNT3N+RuFbU+OcProntQeJpgKXzzTElIh0f4vKXyYFgXz68eWht9Dv/ilDPFLdWD7g4zDXdPmRSLSfDW8hbKBKHu4cTQpw7UxdIanNIHBFYeRa4qMzvGC0NELF0ikczUAhq0JvOU309M9ELIGSmrnvorDvCW238lOrFe7XviUn9JxJ77EmIPI2AgMVRgvcJgrQAavUcKoqO1yNH9OVbIItFtvJkuP4dfrMXjaPb/jNfh6Jf1OsiauwkKhZ8zRm+QLEkOawXHXXkc1Oe+RIaGQJPUl9vNptPDnemUGSf0wrhKYW6veKlcbDCHBNN8wMQVQpQVZDd1Ok73XLWvhvou8nWDCXR5eVu3bod02ImaQIeXCi93IQ90jjkNl/4B1ktsk98bZDr+S1+WhtaaOqD8OrxB3Dh3wqs8W+EFaWSa3u0B1Zvi2H2q7uDrGQFIaMrLu3al3BOlUrUBMEDvkpYgGsq/fKw8zR3P3DpbSz1Byz6pbLmcZuwSd9lHMKB4aQXOVJ8uVF8S4nPvOp2LoBAhIKL2qxUcqS4BBc0SYK8cf9OwbKgqpnEcm6guOCsmXtnAwkef7c118ok4VV19Q4wQIV3ndFggEBwzeibZKDc+Klf9dEjDHtYIhaRmwCUApUt3eSL8anb51LngdsqqJqVksqD4Lm+Z7Z1jSbYLxLpyj9WNrGUgpYnFMWdqtNnJPyprGqoKuK3AzvoR2D60qzd3wYypl4XSyRik5o/NdNZyqmdBAUKZr/XMsfvN8cTMXOZ9wTd5YLVaJM2ADFm8YVPaPjLIucplbhe13D87PUkL2hYZaSWsdpuyN/P+wEkjjWt4avvpbtvFF7MMAZ5pZ88oR4uAzkk99z5NaNk4zeGdXCrnUuB+MyQDseeFQmfTJ0b1+V90xXNDlRX/UpwDZ2BxpRL2hTc8LxhMHzzKmMJXNm3ZinKq2RPIpChdGICnPXkD0qOi8a4kgRbuc6U5XKYJq3W9vw2tGpyfkExv5WcOfO6kNP1fj/leha6E7zLiJlfUijaiF5c2xxUSadZ6N+UQ9yTrBJxbbABfCkUb4aDjvEyhkNKuhAFvkOMP6DUPdChHM8Grwv/Lpyc1C+/mRp3bBKv7WM0w3q+gApIx8fFA76y9aM5lqVjuSPc2QRfFcmpnRKDtP0glfpqyLUCtzfQDcCaE2zJ7P5DjR0HZVgFWMbXYJDA5tieocP5++uHherKDutpCaEhHNtv58DygL+7WQL63or9r7ijpXfQKDMv9xfjzva0dkQukkYbYWHf/hmJmW2JpYtFVdc7kCFl8UTs79pJcKVKJAnTkiYD9sSfQA/azUSWNFNt/SCCba8AlUZhaFZ9kc8BxMEpJC7I2m3zEmJusHYi7GaQ0kgLpPiCsK3Z3L5srFJ5X6zG6c2RZRlrJmx9UdSbo6NBsc8N8Z09QeZr9ThDS9GNrhIG00hCPNa/q5J7H5/BZ3e3E0LGpopMPGnpyElu+7H8DPlWwIPglIf+rTCciVB1YRHmk/egWVxYPH+CpCMijvS2A8g+PxaCpNa0UH5oLsBk2yUz9gTl9iZo5g49r5eAX74aEsRDHO7J1cmCmu00noZOCMUytg/P0MvXN6otr02rWlmV+WUjjFh2HLl5doU3bmWpt0Nd+I9+K2qOOhjWJxfr5H/pQkbpDFW+PxDwYd6+AnEu7hmjJzhardAJ4KhrUYOGVo7epKUy1Lhtd1G/yUBXM3+WYBflWytReM4pcnih/XubUDmqeVM/qpwIBIOXaENzG6ESN1gaiYpXR5bai023y0cRgAPcxZSWKOPRZNtJaR7vbuVUVrj5WGCpqsR8gQFObsVa97exOx2yTn076pQrgj1AnYGVeCkIc8/Xh/pT44xS9WQJPeagr4ocBSGH12j/Arib3SntjqXqPUwckYP8sj+5RdBu+BDWF5gUFhxz40GNdrcTUtJSBDuOyMVMWlz8h4c7CW/I7aDePE2J9jzhoTuWSbLUEKm//boNpOeh1+8eR2n11ltzwb0XBbTfQxnkXrr/FSIiZg737uGpHue20mwqXe/Juk8WQXa52ejq8E9Ig3QuDYGV0vnUm4dTN/XHZtXuGc2T1xeqCn1WiLOcqZRUvr94QByTWdOTPmbZgbavNBlTHLhvaZzHme6x68CUIHwg8v15q7StCwY+foQSuxUzUa36Me+KZekbU0lVz9im+27YEVf/haNc/TewLELFrbyTlRYSqPiZDt61wmttKOe9sFHgErXViIdjXcTXd6iwgYmHnY6uhjxvFax505+urlmhcD+XSPoEW2T6WPRuSNvDtKdW530GxFo5IX90RjJ/YcoOHSxSbeqofxSBfyvujLlMP0Tz26u6WG6kZHUDJ1MagU04WECNDC02lF4Jq2fvyUegzD4XecuyNWqw7oBN4xqcKIBMP1qqVoYibzL3k7hHRCD4zMsHd0EbtEGnOXQqJl43UbZM1MjHdp2T7XguqoEIm43sSATyLBOe6ICO3FXoKhrkaXQ0ojR7eOHjm44UaDKWfvaefcfD+IpbAw7wUkJwyT17mjMV7RWl4VUk8XKzWMnrrzWLXLedlGvEkXs/imR2Ukw8yqHif1veTzkeKbjCVb/zN7+iaKEnrxSQ4RzX0YYaOjH0GQjJt6VY8OKsql55z+7cm3pZysUCYemiFInblVlPbL4ipEyNrgwNi+8PqE5nETItL65ZAkQsE4Q1o+IEeJtAUl8WXtVkqcxdRCH74DXC4Cg8A7gGUaT3G1hlAozPzekEW3stPvH6EyCWCTomb0BW9humsEqDt3DlcXOUMZV1byF4OeaIa4EVCD2Xr0KQ36qTCwqCxyZo/jxrco7/gX9SCqZXmzZDXpd5rMDeoGuX81CpClQhxiJd6x2pKC9+IdCRf8OSglbpX3ETqelowu0+d1znGu6+KYW/PoGJ5ZY3IkQg19KUsZzEtZhxrGeD/KXh8XbsAf/je3xZg9rNM/WroJn/ZqbY2hWKx8LFMyZfEVdgwC0JobHIuuKrRxpR1dPKIC104ukfzp3pKM0WFidTKM+Ah2Nk1K9v5Ap7zSZKMA7MXhmnge1sanPLxeZNuLFrG4HsKaOauOY38iGNXB/oELgvoRYJxAHTsiZHQT2LbHXFYOfdYeiur/NGeht+m3JQnHp2vhxBfQxhOxeQS57zkiRdVcgdPCMLh12rDvdnwOYFXFSOoBnZx9zkIHrvty2Q5/ev5xbPUX955/jpY0+4YZFpw9btlZB3AMaR1sQfArzMVzfmDwQI/J0Zvvm95rmOck0AFyJcEEa6VM7/Opie80npHex+74zYu64DpPJBOsoBk5zLFbkEzbY4FHZ7ctpfLPASoXspOmW8TzjOkSUxEfi5kQkT479dvaY6i275lvpmUUZVLS7gOUHdDQvighyx0KUbY1uTfrL0wOv4dmI9Cm30xYYaY/nNwX0MA14Q1Rru8EN3prYeHLcwaHamIRIoV7B3nKBuAuF7p0uF9MwDpyu='

# ---- ttwid 生成配置 ----
TTWID_URL = 'https://ttwid.bytedance.com/ttwid/union/register/'
TTWID_DATA = '{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com","migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}'

# 抖音 API 的浏览器指纹（a_bogus 需要）
_UA_CHROME = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0'
)


def extract_video_id(url: str) -> str:
    """从各种抖音链接形式里提取视频 aweme_id。"""
    if not url:
        return ''
    for pat in (r'/video/(\d+)', r'modal_id=(\d+)', r'/(\d{18,20})',
                r'aweme_id=(\d+)'):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ''


def _extract_aweme_id_from_text(text: str) -> str:
    if not text:
        return ''
    for pat in (r'/video/(\d+)', r'modal_id=(\d+)', r'aweme_id=(\d+)',
                r'(\d{18,20})'):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ''


async def resolve_short_url(session: aiohttp.ClientSession, url: str) -> str:
    """解析抖音短链，返回标准视频页 URL 或 aweme_id。"""
    try:
        async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            loc = resp.headers.get('Location') or resp.headers.get('location') or ''
            vid = _extract_aweme_id_from_text(loc) or _extract_aweme_id_from_text(str(resp.url))
            if vid:
                return vid
    except Exception as e:
        log.warning('解析短链重定向失败: %s', e)
    return ''


def load_cookies_dict(cookies_path: str | None) -> dict:
    """从 MeTube 的 cookies.txt（Netscape 格式）读取抖音相关 Cookie。"""
    cookies = {}
    if not cookies_path or not os.path.exists(cookies_path):
        return cookies
    try:
        with open(cookies_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7 and 'douyin.com' in parts[0]:
                    cookies[parts[5]] = parts[6]
    except Exception as e:
        log.warning('读取 cookies 失败: %s', e)
    return cookies


async def gen_ttwid(session: aiohttp.ClientSession) -> str | None:
    try:
        async with session.post(TTWID_URL, data=TTWID_DATA,
                                headers={'Content-Type': 'application/json', 'User-Agent': DEFAULT_UA},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await resp.read()
            m = resp.cookies.get('ttwid')
            if m:
                return m.value
    except Exception as e:
        log.warning('生成 ttwid 失败: %s', e)
    return None


async def gen_ms_token(session: aiohttp.ClientSession) -> str:
    """生成真实的 msToken；失败时返回一个格式合法的假 token。"""
    try:
        payload = json.dumps({
            'magic': MSTOKEN_MAGIC,
            'version': MSTOKEN_VERSION,
            'dataType': MSTOKEN_DATATYPE,
            'strData': MSTOKEN_STRDATA,
            'ulr': MSTOKEN_ULR,
            'tspFromClient': int(time.time() * 1000),
        })
        async with session.post(
            MSTOKEN_URL, data=payload,
            headers={'Content-Type': 'application/json; charset=utf-8', 'User-Agent': DEFAULT_UA},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            await resp.read()
            m = resp.cookies.get('msToken')
            if m and len(m.value) >= 100:
                return m.value
    except Exception as e:
        log.warning('生成 msToken 失败: %s', e)
    return gen_fake_ms_token()


def gen_fake_ms_token() -> str:
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
    return ''.join(random.choices(alphabet, k=164))


def gen_webid() -> str:
    """生成一个随机的 19 位 webid（s_v_web_id）。"""
    return str(random.randint(1000000000000000000, 9999999999999999999))


def build_detail_params(aweme_id: str, ms_token: str) -> str:
    """按抖音要求的字段顺序拼出 detail 接口的查询串（用于签名）。"""
    # 顺序必须与签名一致，不能随意改动
    ordered = [
        ('device_platform', 'webapp'),
        ('aid', '6383'),
        ('channel', 'channel_pc_web'),
        ('pc_client_type', '1'),
        ('publish_video_strategy_type', '2'),
        ('pc_libra_divert', 'Windows'),
        ('version_code', '290100'),
        ('version_name', '29.1.0'),
        ('cookie_enabled', 'true'),
        ('screen_width', '1920'),
        ('screen_height', '1080'),
        ('browser_language', 'zh-CN'),
        ('browser_platform', 'Win32'),
        ('browser_name', 'Edge'),
        ('browser_version', '130.0.0.0'),
        ('browser_online', 'true'),
        ('engine_name', 'Blink'),
        ('engine_version', '130.0.0.0'),
        ('os_name', 'Windows'),
        ('os_version', '10'),
        ('cpu_core_num', '12'),
        ('device_memory', '8'),
        ('platform', 'PC'),
        ('downlink', '10'),
        ('effective_type', '4g'),
        ('round_trip_time', '100'),
        ('msToken', ms_token),
        ('aweme_id', aweme_id),
    ]
    return '&'.join(f'{k}={v}' for k, v in ordered)


def sign_detail(params_str: str, ua: str) -> str | None:
    """返回带签名的完整查询串（含 a_bogus 或 X-Bogus）。"""
    if _HAS_ABOGUS:
        try:
            fp = BrowserFingerprintGenerator.generate_fingerprint('Edge')
            signed, _, _, _ = ABogus(fp=fp, user_agent=ua).generate_abogus(params_str, '')
            if signed:
                return signed
        except Exception as e:
            log.warning('生成 a_bogus 失败，回退 X-Bogus: %s', e)
    try:
        signed, _, _ = XBogus(ua).getXBogus(params_str)
        return signed
    except Exception as e:
        log.warning('生成 X-Bogus 失败: %s', e)
        return None


async def fetch_aweme_detail(session: aiohttp.ClientSession, aweme_id: str,
                             cookies: dict, ms_token: str) -> dict | None:
    """带签名拉取 aweme detail 原始 JSON。"""
    params_str = build_detail_params(aweme_id, ms_token)
    signed = sign_detail(params_str, _UA_CHROME)
    if not signed:
        return None
    url = DETAIL_BASE + '?' + signed

    # 带上全部 douyin.com 相关 Cookie（包含登录态 sessionid/sid_guard 等，1080P/4K 往往需要）
    cookie_header = '; '.join(f'{k}={v}' for k, v in cookies.items() if v)
    headers = {
        'User-Agent': _UA_CHROME,
        'Referer': REFERER,
        'Accept': 'application/json, text/plain, */*',
        'Cookie': cookie_header,
    }
    try:
        import yarl
        async with session.get(yarl.URL(url, encoded=True), headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
    except Exception as e:
        log.warning('拉取 aweme detail 失败: %s', e)
        return None
    aweme = (data or {}).get('aweme_detail') or data
    if not aweme or not aweme.get('aweme_id'):
        return None
    return aweme


def extract_best_stream(aweme: dict) -> dict | None:
    """从 aweme_detail 里挑出最高清（分辨率优先、码率次之）的无水印直链。"""
    video = aweme.get('video') or {}
    candidates = []

    for br in video.get('bit_rate') or []:
        addr = br.get('play_addr') or br.get('play_addr_h264') or {}
        urls = addr.get('url_list') or []
        if not urls:
            continue
        w = int(addr.get('width') or br.get('width') or 0)
        h = int(addr.get('height') or br.get('height') or 0)
        candidates.append({
            'url': urls[0],
            'width': w,
            'height': h,
            'bitrate': int(br.get('bit_rate') or 0),
            'filesize': int(addr.get('data_size') or 0),
            'gear': br.get('gear_name') or 'adapt',
        })

    if not candidates:
        for key in ('play_addr', 'play_addr_h264', 'download_addr'):
            addr = video.get(key) or {}
            urls = addr.get('url_list') or []
            if urls:
                candidates.append({
                    'url': urls[0],
                    'width': int(video.get('width') or 0),
                    'height': int(video.get('height') or 0),
                    'bitrate': 0,
                    'filesize': int(addr.get('data_size') or 0),
                    'gear': key,
                })
                break

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c['width'] * c['height'], c['bitrate']), reverse=True)
    best = candidates[0]
    # playwm → play 去掉水印
    best['url'] = best['url'].replace('playwm', 'play')
    return best


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', '_', name).strip()
    return name[:180] or 'douyin'


async def resolve_douyin_video(url: str, cookies_path: str | None = None) -> dict | None:
    """解析抖音视频，返回最高清直链元数据；失败返回 None。"""
    timeout = aiohttp.ClientTimeout(total=30)
    conn = aiohttp.TCPConnector(limit=8)
    async with aiohttp.ClientSession(headers={'User-Agent': DEFAULT_UA, 'Referer': REFERER},
                                     timeout=timeout, connector=conn) as session:
        aweme_id = extract_video_id(url)
        if not aweme_id:
            aweme_id = await resolve_short_url(session, url)
        if not aweme_id:
            log.warning('无法从链接中提取抖音视频 ID: %s', url)
            return None

        cookies = load_cookies_dict(cookies_path)
        ms_token = cookies.get('msToken') or await gen_ms_token(session)
        if not cookies.get('ttwid'):
            ttwid = await gen_ttwid(session)
            if ttwid:
                cookies['ttwid'] = ttwid
        if not cookies.get('s_v_web_id'):
            cookies['s_v_web_id'] = gen_webid()
        # 把 msToken 也并入 cookies，统一在 Cookie 头里携带（含登录态 sessionid/sid_guard 等）
        cookies.setdefault('msToken', ms_token)

        aweme = await fetch_aweme_detail(session, aweme_id, cookies, ms_token)
        if not aweme:
            return None

        stream = extract_best_stream(aweme)
        if not stream:
            log.warning('未能从抖音返回数据中解析到视频流: %s', aweme_id)
            return None

        desc = (aweme.get('desc') or '').strip() or f'抖音视频_{aweme_id}'
        author = (aweme.get('author') or {}).get('nickname') or 'douyin_user'
        return {
            'id': aweme_id,
            'title': desc,
            'author': author,
            'play_url': stream['url'],
            'width': stream['width'],
            'height': stream['height'],
            'filesize': stream['filesize'],
            'gear': stream['gear'],
            'cookies': cookies,
        }


async def download_stream(detail: dict, dest_path: str,
                          progress_cb=None, is_canceled=None) -> str:
    """把直链流式下载到 dest_path，回调 progress_cb(downloaded, total)。"""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + '.part'
    headers = {'User-Agent': DEFAULT_UA, 'Referer': REFERER}
    total = detail.get('filesize') or 0

    async with aiohttp.ClientSession(headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=600)) as session:
        async with session.get(detail['play_url']) as resp:
            if resp.status != 200:
                raise RuntimeError(f'下载直链失败，HTTP 状态码: {resp.status}')
            total = total or int(resp.headers.get('Content-Length') or 0)
            downloaded = 0
            with open(tmp_path, 'wb') as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if is_canceled and is_canceled():
                        raise asyncio.CancelledError()
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        await progress_cb(downloaded, total)
    os.replace(tmp_path, dest_path)
    return dest_path
