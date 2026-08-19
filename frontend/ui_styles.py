"""Streamlit 界面的集中 CSS 设计系统。

业务渲染与视觉规则分离：Python 文件决定展示什么，本文件决定颜色、间距、圆角
和响应式布局。``:root`` 变量是设计令牌，改主色或圆角时优先改变量而非搜索替换
几十个选择器。选择器依赖 Streamlit 的 data-testid，升级 Streamlit 后应进行
浏览器回归检查；``!important`` 仅用于覆盖框架内联样式。
"""


# 作为一个完整 <style> 块注入页面。这里不是 Python f-string，CSS 花括号无需转义。
APP_CSS = """
<style>
:root {
    --nm-primary: #0b57d0;
    --nm-primary-hover: #0842a0;
    --nm-primary-soft: #d3e3fd;
    --nm-on-primary-soft: #041e49;
    --nm-secondary-soft: #e8def8;
    --nm-text: #1f1f1f;
    --nm-muted: #5f6368;
    --nm-faint: #747775;
    --nm-outline: #c7c7c7;
    --nm-outline-soft: #e1e3e1;
    --nm-surface: #ffffff;
    --nm-surface-low: #f8fafd;
    --nm-surface-mid: #f1f4f9;
    --nm-sidebar: #f0f4f9;
    --nm-success: #146c2e;
    --nm-success-bg: #c4eed0;
    --nm-warning: #7d5700;
    --nm-warning-bg: #fff3c4;
    --nm-danger: #b3261e;
    --nm-danger-bg: #f9dedc;
    --nm-radius-sm: 10px;
    --nm-radius-md: 16px;
    --nm-radius-lg: 24px;
    --nm-shadow: 0 1px 2px rgba(60, 64, 67, 0.08),
                 0 1px 3px 1px rgba(60, 64, 67, 0.05);
    --nm-shadow-raised: 0 2px 8px rgba(60, 64, 67, 0.12);
}

*, *::before, *::after {
    box-sizing: border-box;
}

html, body, [class*="css"] {
    font-family: "Google Sans", "Segoe UI", Roboto, Arial,
                 "Noto Sans SC", sans-serif;
    color: var(--nm-text);
}

.stApp {
    background: var(--nm-surface-low);
}

#MainMenu, footer,
[data-testid="stDecoration"],
[data-testid="stStatusWidget"] {
    display: none !important;
}

header[data-testid="stHeader"] {
    height: 2.25rem;
    background: transparent;
    pointer-events: none;
    z-index: 1000;
}

header[data-testid="stHeader"] button,
header[data-testid="stHeader"] [role="button"] {
    pointer-events: auto;
}

/* Keep Streamlit's sidebar controls reachable in both states. */
[data-testid="stSidebarCollapseButton"],
[data-testid="stSidebarCollapsedControl"],
button[data-testid="stExpandSidebarButton"] {
    display: flex !important;
    visibility: visible !important;
    opacity: 1 !important;
    pointer-events: auto !important;
}

[data-testid="stSidebarCollapsedControl"],
button[data-testid="stExpandSidebarButton"] {
    position: fixed !important;
    top: 0.65rem !important;
    left: 0.65rem !important;
    z-index: 1001 !important;
    width: 40px;
    height: 40px;
    align-items: center;
    justify-content: center;
    border-radius: 20px;
    background: var(--nm-surface);
    box-shadow: var(--nm-shadow);
}

[data-testid="stAppDeployButton"] {
    display: none !important;
}

.block-container {
    max-width: 1240px;
    padding: 2rem 2.25rem 5rem;
}

/* Sidebar: one calm, persistent place for product-level navigation. */
section[data-testid="stSidebar"] {
    width: 272px !important;
    background: var(--nm-sidebar);
    border-right: 0;
}

section[data-testid="stSidebar"] > div {
    padding-top: 1.15rem;
}

section[data-testid="stSidebar"] .block-container {
    padding: 0 0.85rem 1.5rem;
}

.nm-sidebar-brand {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    margin: 0.2rem 0.35rem 1.5rem;
    padding: 0.35rem 0.2rem;
}

.nm-brand-mark {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border-radius: 14px;
    background: linear-gradient(145deg, #0b57d0, #6d8ff0);
    color: #ffffff;
    font-size: 18px;
    font-weight: 700;
    box-shadow: 0 2px 6px rgba(11, 87, 208, 0.22);
}

.nm-brand-name {
    color: var(--nm-text);
    font-size: 18px;
    font-weight: 650;
    letter-spacing: -0.01em;
}

.nm-brand-area {
    margin-top: 1px;
    color: var(--nm-muted);
    font-size: 12px;
}

.nm-nav-label {
    margin: 1.25rem 0.75rem 0.4rem;
    color: var(--nm-faint);
    font-size: 11px;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}

.nm-nav-label-secondary {
    margin-top: 1.2rem;
}

.nm-sidebar-book {
    margin: 1rem 0.3rem 0.25rem;
    padding: 0.9rem 1rem;
    border-radius: var(--nm-radius-md);
    background: rgba(255, 255, 255, 0.72);
    color: var(--nm-muted);
    font-size: 12px;
    line-height: 1.45;
}

.nm-sidebar-book strong {
    display: block;
    overflow: hidden;
    margin-bottom: 0.2rem;
    color: var(--nm-text);
    font-size: 13px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
}

section[data-testid="stSidebar"] div.stButton > button {
    justify-content: flex-start;
    min-height: 44px;
    margin: 1px 0;
    padding: 0 1rem;
    border: 0;
    border-radius: 22px;
    background: transparent;
    color: #303030;
    box-shadow: none;
    font-weight: 500;
}

section[data-testid="stSidebar"] div.stButton > button p {
    width: 100%;
    text-align: left;
}

section[data-testid="stSidebar"] div.stButton > button:hover {
    background: rgba(68, 71, 70, 0.08);
}

section[data-testid="stSidebar"] div.stButton > button:disabled {
    background: var(--nm-primary-soft);
    color: var(--nm-on-primary-soft);
    opacity: 1;
}

/* A lightweight utility strip; the product brand now lives in the sidebar. */
.nm-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    min-height: 34px;
    margin-bottom: 1.5rem;
}

.nm-context {
    color: var(--nm-muted);
    font-size: 13px;
}

.nm-status {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    min-height: 30px;
    padding: 0 0.75rem;
    border-radius: 16px;
    background: #e6f4ea;
    color: var(--nm-success);
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
}

.nm-status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--nm-success);
}

.nm-status.is-offline {
    background: var(--nm-danger-bg);
    color: var(--nm-danger);
}

.nm-status.is-offline .nm-status-dot {
    background: var(--nm-danger);
}

.nm-page-heading {
    margin: 0 0 1.6rem;
}

.nm-page-heading h1 {
    max-width: 900px;
    margin: 0;
    color: var(--nm-text);
    font-size: clamp(28px, 3.2vw, 40px);
    font-weight: 560;
    letter-spacing: -0.035em;
    line-height: 1.16;
}

.nm-page-heading p {
    max-width: 720px;
    margin: 0.55rem 0 0;
    color: var(--nm-muted);
    font-size: 14px;
    line-height: 1.6;
}

.nm-workspace-heading {
    margin: 2.25rem 0 1rem;
}

.nm-workspace-heading .nm-eyebrow {
    margin-bottom: 0.35rem;
    color: var(--nm-primary);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.09em;
}

.nm-workspace-heading h2 {
    margin: 0;
    color: var(--nm-text);
    font-size: 24px;
    font-weight: 560;
    letter-spacing: -0.02em;
}

.nm-workspace-heading p {
    margin: 0.35rem 0 0;
    color: var(--nm-muted);
    font-size: 13px;
}

h1 a, h2 a, h3 a, h4 a {
    display: none !important;
}

h3 {
    margin-top: 1.35rem !important;
    color: var(--nm-text) !important;
    font-size: 17px !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
}

hr {
    display: none;
}

/* Native controls: tonal by default; one primary action stays visually primary. */
div.stButton > button {
    width: 100%;
    min-height: 42px;
    padding: 0 1.05rem;
    border: 0;
    border-radius: 21px;
    background: var(--nm-primary-soft);
    color: var(--nm-on-primary-soft);
    box-shadow: none;
    font-weight: 600;
    transition: background 120ms ease, box-shadow 120ms ease,
                transform 120ms ease;
}

button[data-testid^="stBaseButton"] p,
details[data-testid="stExpander"] summary p {
    margin: 0 !important;
    line-height: 1.25 !important;
}

div.stButton > button:hover,
div.stButton > button:focus {
    border: 0;
    background: #b7d1f8;
    color: var(--nm-on-primary-soft);
    box-shadow: var(--nm-shadow);
}

div.stButton > button:active {
    transform: scale(0.99);
}

div.stButton > button:disabled {
    border: 0;
    background: #e3e3e3;
    color: #8a8a8a;
    opacity: 0.75;
}

button[data-testid="stBaseButton-primary"]:not(:disabled),
button[data-testid="stBaseButton-primaryFormSubmit"]:not(:disabled) {
    background: var(--nm-primary) !important;
    color: #ffffff !important;
}

button[data-testid="stBaseButton-primary"]:not(:disabled):hover,
button[data-testid="stBaseButton-primaryFormSubmit"]:not(:disabled):hover {
    background: var(--nm-primary-hover) !important;
}

button[data-testid="stBaseButton-primaryFormSubmit"] {
    min-height: 42px;
    border: 0;
    border-radius: 21px;
    font-weight: 600;
}

button[data-testid="stBaseButton-primary"]:disabled,
button[data-testid="stBaseButton-primaryFormSubmit"]:disabled {
    background: #e2e5e9 !important;
    color: #80868b !important;
    box-shadow: none !important;
    opacity: 1 !important;
}

/* File uploader, inputs and selectors share the same soft geometry. */
div[data-testid="stFileUploader"] {
    margin-bottom: 0.5rem;
}

div[data-testid="stFileUploader"] > label,
div[data-testid="stTextInput"] > label,
div[data-testid="stTextArea"] > label,
div[data-testid="stSelectbox"] > label {
    color: var(--nm-text);
    font-size: 13px;
    font-weight: 600;
}

[data-testid="stWidgetLabel"] {
    align-items: center;
    min-height: 24px;
}

[data-testid="stWidgetLabel"] [data-testid="stTooltipIcon"] {
    display: inline-flex !important;
    width: 18px !important;
    height: 18px !important;
    margin-left: 0.35rem;
    align-items: center !important;
    justify-content: center !important;
    overflow: hidden !important;
    border-radius: 50%;
}

[data-testid="stWidgetLabel"] [data-testid="stTooltipIcon"] [data-testid="stTooltipHoverTarget"],
[data-testid="stWidgetLabel"] [data-testid="stTooltipIcon"] button {
    display: inline-flex !important;
    width: 18px !important;
    min-width: 18px !important;
    height: 18px !important;
    min-height: 18px !important;
    padding: 0 !important;
    align-items: center !important;
    justify-content: center !important;
    border: 0 !important;
    border-radius: 50% !important;
    background: transparent !important;
    color: var(--nm-muted) !important;
    box-shadow: none !important;
    line-height: 1 !important;
    overflow: hidden !important;
}

div[data-testid="stFileUploader"] section {
    display: flex !important;
    min-height: 124px;
    padding: 1.25rem;
    align-items: center !important;
    gap: 1rem;
    border: 1.5px dashed #9aa0a6;
    border-radius: var(--nm-radius-lg);
    background: var(--nm-surface);
}

div[data-testid="stFileUploader"] section:hover {
    border-color: var(--nm-primary);
    background: #f6f9fe;
}

div[data-testid="stFileUploader"] section button[data-testid="stBaseButton-secondary"] {
    min-height: 40px;
    padding: 0 1rem;
    border: 0;
    border-radius: 20px;
    background: var(--nm-primary-soft);
    color: var(--nm-on-primary-soft);
}

div[data-testid="stFileUploader"] section button[data-testid="stBaseButton-secondary"] p {
    font-size: 0;
}

div[data-testid="stFileUploader"] section button[data-testid="stBaseButton-secondary"] p::after {
    content: "选择文件";
    font-size: 14px;
}

[data-testid="stFileUploaderDropzoneInstructions"] {
    display: flex !important;
    width: auto !important;
    flex: 0 1 auto !important;
    align-items: center !important;
    margin: 0 !important;
}

[data-testid="stFileUploaderDropzoneInstructions"] span {
    color: var(--nm-muted) !important;
    font-size: 0 !important;
}

[data-testid="stFileUploaderDropzoneInstructions"] span::after {
    content: "单个文件不超过 20 MB · TXT";
    font-size: 13px;
}

div[data-baseweb="input"] > div,
div[data-baseweb="select"] > div,
div[data-baseweb="textarea"] > div {
    min-height: 44px;
    border-color: var(--nm-outline) !important;
    border-radius: var(--nm-radius-sm) !important;
    background: var(--nm-surface) !important;
}

/* Workspaces behave like a compact tool switcher, not a survey form. */
div[data-testid="stRadio"] > label {
    display: none;
}

div[data-testid="stRadio"] div[role="radiogroup"] {
    display: flex;
    width: fit-content;
    max-width: 100%;
    gap: 4px;
    margin: 0.2rem 0 0.4rem;
    padding: 4px;
    overflow-x: auto;
    border-radius: 24px;
    background: var(--nm-surface-mid);
}

div[data-testid="stRadio"] div[role="radiogroup"] > label {
    display: flex !important;
    align-items: center !important;
    justify-content: center;
    min-height: 38px;
    margin: 0;
    padding: 0 0.9rem !important;
    border-radius: 19px;
    line-height: 1;
    white-space: nowrap;
}

label[data-testid="stRadioOption"]
div:has(> [data-testid="stMarkdownContainer"])
> div:not([data-testid="stMarkdownContainer"]) {
    display: none !important;
}

label[data-testid="stRadioOption"] [data-testid="stMarkdownContainer"] {
    display: flex;
    height: 100%;
    align-items: center;
    justify-content: center;
}

div[data-testid="stRadio"] div[role="radiogroup"] > label > div {
    display: flex;
    align-items: center;
    min-height: 38px;
}

div[data-testid="stRadio"] div[role="radiogroup"] p {
    display: flex;
    align-items: center;
    min-height: 38px;
    margin: 0 !important;
    line-height: 1.2 !important;
}

div[data-testid="stRadio"] div[role="radiogroup"] > label:has(input:checked) {
    background: var(--nm-surface);
    box-shadow: var(--nm-shadow);
    color: var(--nm-primary);
}

/* Cards, metrics, expanders and alerts. */
div[data-testid="metric-container"],
div[data-testid="stMetric"] {
    min-height: 88px;
    padding: 0.95rem 1rem;
    border: 0;
    border-radius: var(--nm-radius-md);
    background: var(--nm-surface-mid);
}

div[data-testid="metric-container"] label,
div[data-testid="stMetric"] [data-testid="stMetricLabel"] {
    color: var(--nm-muted);
    font-size: 11px;
    font-weight: 600;
}

div[data-testid="metric-container"] [data-testid="stMetricValue"],
div[data-testid="stMetric"] [data-testid="stMetricValue"] {
    color: var(--nm-text);
    font-size: 26px !important;
    font-weight: 540;
    line-height: 1.15;
    overflow-wrap: anywhere;
    white-space: normal;
}

details[data-testid="stExpander"] {
    margin: 0.6rem 0;
    border: 1px solid var(--nm-outline-soft);
    border-radius: var(--nm-radius-md);
    background: var(--nm-surface);
    box-shadow: none;
}

details[data-testid="stExpander"] > summary {
    min-height: 48px;
    padding: 0 0.85rem;
    align-items: center;
    color: var(--nm-text);
    font-weight: 600;
}

details[data-testid="stExpander"][open] {
    box-shadow: var(--nm-shadow);
}

.nm-disclosure {
    margin: 0.65rem 0;
    padding: 0.1rem 0.8rem;
    border: 1px solid var(--nm-outline-soft);
    border-radius: var(--nm-radius-sm);
    background: var(--nm-surface-low);
    color: var(--nm-muted);
    font-size: 12px;
    line-height: 1.55;
}

.nm-disclosure summary {
    min-height: 38px;
    display: flex;
    align-items: center;
    color: var(--nm-text);
    cursor: pointer;
    font-weight: 600;
}

.nm-disclosure p {
    margin: 0 0 0.55rem;
}

.nm-disclosure a {
    color: var(--nm-primary);
    text-decoration: none;
}

.nm-disclosure-links {
    margin: 0 0 0.7rem;
}

.nm-provider-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.65rem;
    padding: 0.9rem 1rem;
    border-radius: var(--nm-radius-md);
    background: var(--nm-surface-mid);
}

.nm-provider-summary > div:first-child {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 0.15rem;
}

.nm-provider-summary strong {
    color: var(--nm-text);
    font-size: 14px;
    font-weight: 600;
}

.nm-provider-summary span {
    overflow: hidden;
    color: var(--nm-muted);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.nm-provider-state {
    flex: 0 0 auto;
    padding: 0.3rem 0.6rem;
    border-radius: 999px;
    background: #fce8e6;
    color: #a50e0e;
    font-size: 12px;
    font-weight: 600;
}

.nm-provider-state.is-ready {
    background: var(--nm-success-bg);
    color: var(--nm-success);
}

div[data-testid="stAlert"] {
    border: 0;
    border-radius: var(--nm-radius-md);
    box-shadow: none;
    overflow: hidden;
}

div[data-testid="stAlert"] p {
    font-size: 13px;
    line-height: 1.55;
}

div[data-testid="stProgress"] > div > div {
    border-radius: 999px;
}

.nm-library-card {
    margin: 0.65rem 0 0.3rem;
    padding: 1.15rem 1.25rem;
    border: 1px solid var(--nm-outline-soft);
    border-radius: var(--nm-radius-md);
    background: var(--nm-surface);
    box-shadow: var(--nm-shadow);
}

.nm-library-card strong {
    display: block;
    overflow: hidden;
    color: var(--nm-text);
    font-size: 16px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.nm-library-meta {
    margin-top: 0.4rem;
    color: var(--nm-muted);
    font-size: 12px;
}

.nm-file-note {
    min-height: 42px;
    display: flex;
    align-items: center;
    color: var(--nm-muted);
    font-size: 13px;
}

.st-key-settings_generation_card,
.st-key-settings_embedding_card {
    height: 100%;
    border-color: var(--nm-outline-soft) !important;
    border-radius: var(--nm-radius-md) !important;
    background: var(--nm-surface) !important;
}

div[data-testid="stForm"]:has(.st-key-settings_generation_card)
div[data-testid="stHorizontalBlock"] {
    align-items: stretch;
}

div[data-testid="stForm"]:has(.st-key-settings_generation_card)
div[data-testid="stColumn"] {
    display: flex;
}

div[data-testid="stForm"]:has(.st-key-settings_generation_card)
div[data-testid="stColumn"] > div {
    width: 100%;
}

div[data-testid="stForm"]:has(.st-key-settings_generation_card)
div[data-testid="stColumn"]
> div[data-testid="stVerticalBlock"]
> div[data-testid="stLayoutWrapper"] {
    min-height: 100%;
    flex: 1 1 auto;
}

@media (max-width: 1100px) {
    div[data-testid="stForm"]:has(.st-key-settings_generation_card)
    div[data-testid="stHorizontalBlock"] {
        flex-wrap: wrap;
    }

    div[data-testid="stForm"]:has(.st-key-settings_generation_card)
    div[data-testid="stColumn"] {
        width: 100% !important;
        flex: 1 1 100% !important;
    }

    .st-key-nm_two_pane_summary
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"],
    .st-key-nm_two_pane_characters
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"],
    .st-key-nm_two_pane_rag
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"],
    .st-key-nm_two_pane_report
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"] {
        flex-wrap: wrap;
    }

    .st-key-nm_two_pane_summary
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"]
    > div[data-testid="stColumn"],
    .st-key-nm_two_pane_characters
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"]
    > div[data-testid="stColumn"],
    .st-key-nm_two_pane_rag
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"]
    > div[data-testid="stColumn"],
    .st-key-nm_two_pane_report
    > div[data-testid="stLayoutWrapper"]
    > div[data-testid="stHorizontalBlock"]
    > div[data-testid="stColumn"] {
        width: 100% !important;
        flex: 1 1 100% !important;
    }
}

.nm-success {
    margin: 1.2rem 0;
    padding: 0.8rem 1rem;
    border-radius: var(--nm-radius-md);
    background: var(--nm-success-bg);
    color: var(--nm-success);
    font-size: 13px;
    font-weight: 600;
}

.nm-table-wrap {
    margin-top: 0.75rem;
    overflow-x: auto;
    border: 1px solid var(--nm-outline-soft);
    border-radius: var(--nm-radius-md);
    background: var(--nm-surface);
}

.nm-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}

.nm-table th {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--nm-outline-soft);
    background: var(--nm-surface-mid);
    color: var(--nm-muted);
    font-size: 11px;
    font-weight: 650;
    text-align: left;
}

.nm-table td {
    padding: 0.8rem 1rem;
    border-bottom: 1px solid #edf0f2;
    vertical-align: top;
}

.nm-table tr:last-child td {
    border-bottom: 0;
}

.nm-number {
    width: 72px;
    color: var(--nm-muted) !important;
    font-variant-numeric: tabular-nums;
}

.nm-count {
    width: 120px;
    color: var(--nm-muted) !important;
    text-align: right;
    font-variant-numeric: tabular-nums;
}

.nm-reader {
    min-height: 460px;
    margin-top: 0.75rem;
    padding: clamp(1.4rem, 4vw, 3.5rem);
    border: 1px solid var(--nm-outline-soft);
    border-radius: var(--nm-radius-lg);
    background: var(--nm-surface);
    box-shadow: var(--nm-shadow);
}

.nm-reader h2 {
    max-width: 760px;
    margin: 0 auto 1.8rem;
    color: var(--nm-text);
    font-size: 23px;
    font-weight: 600;
    letter-spacing: -0.02em;
}

.nm-reader-body {
    max-width: 760px;
    margin: 0 auto;
    color: #303134;
    font-family: "Noto Serif SC", "Songti SC", SimSun, serif;
    font-size: 17px;
    line-height: 2;
    white-space: pre-wrap;
    word-break: break-word;
}

/* Streamlit's column layout becomes a clean supporting-pane layout. */
div[data-testid="stHorizontalBlock"] {
    gap: 1rem;
}

small, .stCaptionContainer {
    color: var(--nm-muted) !important;
}

@media (max-width: 900px) {
    .block-container {
        padding: 1.5rem 1.25rem 4rem;
    }

    .nm-page-heading h1 {
        font-size: 30px;
    }
}

@media (max-width: 700px) {
    [data-testid="stSidebarCollapsedControl"],
    button[data-testid="stExpandSidebarButton"] {
        right: 0.65rem !important;
        left: auto !important;
    }

    section[data-testid="stSidebar"] {
        width: min(86vw, 288px) !important;
    }

    .block-container {
        padding: 1.1rem 0.9rem 3rem;
    }

    .nm-topbar {
        margin-bottom: 1rem;
    }

    .nm-context {
        display: none;
    }

    .nm-page-heading {
        margin-bottom: 1.2rem;
    }

    .nm-page-heading h1 {
        font-size: 27px;
    }

    div[data-testid="column"],
    div[data-testid="stColumn"] {
        width: 100% !important;
        flex: 1 1 100% !important;
    }

    div[data-testid="stRadio"] div[role="radiogroup"] {
        width: 100%;
        border-radius: var(--nm-radius-md);
    }

    div[data-testid="stFileUploader"] section {
        min-height: 132px;
        flex-direction: column;
        align-items: flex-start !important;
        justify-content: center;
    }

    .nm-reader {
        padding: 1.25rem 1rem;
        border-radius: var(--nm-radius-md);
    }

    .nm-reader-body {
        font-size: 16px;
        line-height: 1.9;
    }
}
</style>
"""
