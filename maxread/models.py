from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class PaperFigure:
    asset: str
    caption: str
    tex_file: str = ""
    label: str = ""
    figure_index: int = 0
    asset_index: int = 0
    row: int = 0
    col: int = 0


@dataclass
class ArticleImage:
    url: str
    local_path: Optional[Path]
    caption: str = ""
    alt: str = ""
    source_index: int = 0


@dataclass
class ArticleSection:
    title: str
    level: int
    blocks: List[str] = field(default_factory=list)


@dataclass
class ArticleBundle:
    article_id: str
    url: str
    title: str
    author: str = ""
    published: str = ""
    site_name: str = ""
    text: str = ""
    sections: List[str] = field(default_factory=list)
    section_blocks: List[ArticleSection] = field(default_factory=list)
    images: List[ArticleImage] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    code_blocks: List[str] = field(default_factory=list)
    math_blocks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PaperRef:
    paper_id: str
    url: str


@dataclass
class ArxivMetadata:
    paper_id: str
    title: str
    authors: List[str]
    summary: str
    published: str
    updated: str
    categories: List[str]
    pdf_url: str
    abs_url: str


@dataclass
class PaperBundle:
    metadata: ArxivMetadata
    pdf_path: Optional[Path]
    source_path: Optional[Path]
    source_dir: Optional[Path]
    source_text: str
    pdf_text: str
    source_tree: str = ""
    source_assets: List[str] = field(default_factory=list)
    source_captions: List[str] = field(default_factory=list)
    source_figures: List[PaperFigure] = field(default_factory=list)
    source_tables: List[str] = field(default_factory=list)
    source_macros: Dict[str, str] = field(default_factory=dict)
    source_latex_macros: Dict[str, str] = field(default_factory=dict)
    source_latex_arg_macros: Dict[str, str] = field(default_factory=dict)
    parse_warnings: List[str] = field(default_factory=list)


@dataclass
class FeishuEvent:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    sender_id: str
    content: str
    raw: Dict[str, Any]
    mentioned_bot: bool = False
