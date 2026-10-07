"""Attach local interaction assets to pen.dev HTML exports; safe to rerun."""
from pathlib import Path
root = Path(__file__).parent
for name in ['bi8Au','GXXh7','lX21L','qpClT','V9YZY','b4HLy','ULIfr','ElfMF']:
    p=root/(name+'.html')
    html=p.read_text(encoding='utf-8').replace('lang="en"','lang="zh-CN"')
    if 'prototype.css' not in html:
        html=html.replace('</head>','<link rel="stylesheet" href="prototype.css">\n</head>')
    if 'prototype.js' not in html:
        html=html.replace('</body>','<script src="prototype.js"></script>\n</body>')
    p.write_text(html,encoding='utf-8')
