from pathlib import Path
root=Path(__file__).parent
for name in ['library','reader','summary','characters','ask','report']:
    p=root/(name+'.html')
    text=p.read_text(encoding='utf-8').replace('lang="en"','lang="zh-CN"')
    if 'app.css' not in text:text=text.replace('</head>','<link rel="stylesheet" href="app.css">\n</head>')
    if 'app.js' not in text:text=text.replace('</body>','<script src="app.js"></script>\n</body>')
    if 'reader.js' not in text:text=text.replace('<script src="app.js">','<script src="reader.js"></script>\n<script src="app.js">')
    if 'reader.css' not in text:text=text.replace('</head>','<link rel="stylesheet" href="reader.css">\n</head>')
    p.write_text(text,encoding='utf-8')
