from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# Ministry of Education "Project 985" list (39 institutions):
# https://www.moe.gov.cn/srcsite/A22/s7065/200612/t20061206_128833.html
PROJECT_985 = (
    "北京大学", "中国人民大学", "清华大学", "北京航空航天大学", "北京理工大学", "中国农业大学", "北京师范大学", "中央民族大学",
    "南开大学", "天津大学", "大连理工大学", "东北大学", "吉林大学", "哈尔滨工业大学", "复旦大学", "同济大学",
    "上海交通大学", "华东师范大学", "南京大学", "东南大学", "浙江大学", "中国科学技术大学", "厦门大学", "山东大学",
    "中国海洋大学", "武汉大学", "华中科技大学", "湖南大学", "中南大学", "国防科学技术大学", "中山大学", "华南理工大学",
    "四川大学", "电子科技大学", "重庆大学", "西安交通大学", "西北工业大学", "西北农林科技大学", "兰州大学",
)

# C9 members are the nine first-batch Project 985 universities. Official
# member wording is also published by Shanghai Jiao Tong University:
# https://jwc.sjtu.edu.cn/info/1026/2243.htm
C9 = (
    "北京大学", "清华大学", "浙江大学", "复旦大学", "上海交通大学",
    "南京大学", "中国科学技术大学", "哈尔滨工业大学", "西安交通大学",
)

_ALIASES = {
    "北京大学": ("北大", "PKU", "Peking University"),
    "中国人民大学": ("人大", "RUC", "Renmin University of China"),
    "清华大学": ("清华", "THU", "Tsinghua University"),
    "北京航空航天大学": ("北航", "BUAA", "Beihang University"),
    "北京理工大学": ("北理工", "BIT", "Beijing Institute of Technology"),
    "中国农业大学": ("中国农大", "CAU", "China Agricultural University"),
    "北京师范大学": ("北师大", "BNU", "Beijing Normal University"),
    "中央民族大学": ("中央民大", "Minzu University of China"),
    "南开大学": ("南开", "NKU", "Nankai University"),
    "天津大学": ("天大", "TJU", "Tianjin University"),
    "大连理工大学": ("大工", "DUT", "Dalian University of Technology"),
    "东北大学": ("NEU China",),
    "吉林大学": ("吉大", "JLU", "Jilin University"),
    "哈尔滨工业大学": ("哈工大", "HIT", "Harbin Institute of Technology"),
    "复旦大学": ("复旦", "FDU", "Fudan University"),
    "同济大学": ("同济", "Tongji University"),
    "上海交通大学": ("上海交大", "上交", "SJTU", "Shanghai Jiao Tong University"),
    "华东师范大学": ("华师大", "ECNU", "East China Normal University"),
    "南京大学": ("南京大学", "NJU", "Nanjing University"),
    "东南大学": ("东南大学", "SEU", "Southeast University"),
    "浙江大学": ("浙大", "ZJU", "Zhejiang University"),
    "中国科学技术大学": ("中国科大", "中科大", "USTC", "University of Science and Technology of China"),
    "厦门大学": ("厦大", "XMU", "Xiamen University"),
    "山东大学": ("山东大学", "SDU", "Shandong University"),
    "中国海洋大学": ("中国海大", "OUC", "Ocean University of China"),
    "武汉大学": ("武大", "WHU", "Wuhan University"),
    "华中科技大学": ("华科", "HUST", "Huazhong University of Science and Technology"),
    "湖南大学": ("湖南大学", "HNU", "Hunan University"),
    "中南大学": ("中南大学", "CSU", "Central South University"),
    "国防科学技术大学": ("国防科技大学", "国防科大", "NUDT", "National University of Defense Technology"),
    "中山大学": ("中山大学", "SYSU", "Sun Yat-sen University"),
    "华南理工大学": ("华南理工", "华工", "SCUT", "South China University of Technology"),
    "四川大学": ("四川大学", "Sichuan University"),
    "电子科技大学": ("电子科大", "成电", "UESTC", "University of Electronic Science and Technology of China"),
    "重庆大学": ("重庆大学", "CQU", "Chongqing University"),
    "西安交通大学": ("西安交大", "西交", "XJTU", "Xi'an Jiaotong University", "Xian Jiaotong University"),
    "西北工业大学": ("西工大", "NPU", "Northwestern Polytechnical University"),
    "西北农林科技大学": ("西农", "NWAFU", "Northwest A&F University"),
    "兰州大学": ("兰大", "LZU", "Lanzhou University"),
}

_UNKNOWN = {"", "unknown", "未知", "未提供", "—", "-", "none", "n/a"}


@dataclass(frozen=True)
class InstitutionTags:
    is_985: str
    is_c9: str
    matched_schools: tuple[str, ...] = ()


def classify_institution(school: str, *, applicable: bool = True) -> InstitutionTags:
    if not applicable:
        return InstitutionTags("不适用", "不适用")
    raw = unicodedata.normalize("NFKC", str(school or "")).strip()
    if raw.casefold() in _UNKNOWN:
        return InstitutionTags("未知", "未知")
    parts = [_clean_part(part) for part in re.split(r"[|｜,，;；/\n()（）]+", raw)]
    parts = [part for part in parts if part]
    matches: list[str] = []
    for canonical in PROJECT_985:
        aliases = (canonical, *_ALIASES.get(canonical, ()))
        if any(_part_matches(part, alias) for part in parts for alias in aliases):
            matches.append(canonical)
    unique = tuple(dict.fromkeys(matches))
    if not unique:
        return InstitutionTags("否", "否")
    return InstitutionTags(
        "是",
        "是" if any(name in C9 for name in unique) else "否",
        unique,
    )


def extract_rank_feature(academic_display: str, *, applicable: bool = True) -> str:
    if not applicable:
        return "不适用"
    text = unicodedata.normalize("NFKC", str(academic_display or "")).strip()
    if not text or text.casefold() in _UNKNOWN or "排名未提供" in text:
        return "未提供"
    values: list[str] = []
    for current, total in re.findall(r"(?:排名\s*)?(?:第\s*)?(\d+)\s*(?:/|／|名?\s*(?:共|of))\s*(\d+)", text, flags=re.I):
        values.append(f"第 {int(current)} / {int(total)}")
    for percent in re.findall(r"(?:Top|前)\s*(\d+(?:\.\d+)?)\s*%", text, flags=re.I):
        values.append(f"Top {percent}%")
    for current in re.findall(r"(?:专业)?排名\s*第?\s*(\d+)\s*名?", text):
        marker = f"第 {int(current)} 名"
        if not any(value.startswith(f"第 {int(current)} /") for value in values):
            values.append(marker)
    return " · ".join(dict.fromkeys(values)) or "未提供"


def _clean_part(value: str) -> str:
    text = re.sub(r"^(?:本科|硕士|博士|就读于|现就读于|毕业于)\s*", "", value.strip(), flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" .")


def _part_matches(part: str, alias: str) -> bool:
    if not part or not alias:
        return False
    if alias.isascii() and len(alias) <= 6:
        return bool(re.fullmatch(re.escape(alias), part, flags=re.I))
    return part.casefold() == alias.casefold()
