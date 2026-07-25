"""CSS styles for the Streamlit interface."""


APP_CSS = """
<style>
:root {
    --nm-blue: #1a73e8;
    --nm-blue-hover: #1557b0;
    --nm-text: #202124;
    --nm-muted: #5f6368;
    --nm-border: #dadce0;
    --nm-surface: #ffffff;
    --nm-workspace: #f8fafd;
    --nm-success: #137333;
    --nm-success-bg: #e6f4ea;
    --nm-danger: #b3261e;
}

html, body, [class*="css"] {
    font-family: "Google Sans", Roboto, Arial, "Noto Sans SC", sans-serif;
    color: var(--nm-text);
}

.stApp {
    background: var(--nm-workspace);
}

#MainMenu, footer {
    visibility: hidden;
}

[data-testid="stDecoration"] {
    display: none;
}

header[data-testid="stHeader"] {
    background: transparent;
}

.block-container {
    max-width: 1120px;
    padding-top: 2.25rem;
    padding-bottom: 4rem;
}

.nm-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    min-height: 48px;
    padding-bottom: 1.5rem;
    border-bottom: 1px solid var(--nm-border);
}

.nm-brand {
    display: flex;
    align-items: center;
    gap: 0.75rem;
}

.nm-brand-mark {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border-radius: 8px;
    background: var(--nm-blue);
    color: #ffffff;
    font-size: 19px;
    font-weight: 700;
}

.nm-brand-name {
    color: var(--nm-text);
    font-size: 20px;
    font-weight: 600;
    line-height: 1.15;
}

.nm-brand-area {
    color: var(--nm-muted);
    font-size: 12px;
    line-height: 1.3;
}

.nm-status {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    color: var(--nm-muted);
    font-size: 13px;
    white-space: nowrap;
}

.nm-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--nm-success);
}

.nm-status.is-offline .nm-status-dot {
    background: var(--nm-danger);
}

.nm-page-heading {
    margin: 2rem 0 1.25rem;
}

.nm-page-heading h1 {
    margin: 0;
    color: var(--nm-text);
    font-size: 28px;
    font-weight: 500;
    letter-spacing: 0;
    line-height: 1.25;
}

.nm-page-heading p {
    margin: 0.45rem 0 0;
    color: var(--nm-muted);
    font-size: 14px;
}

div[data-testid="stFileUploader"] {
    margin-bottom: 0.25rem;
}

div[data-testid="stFileUploader"] > label {
    color: var(--nm-text);
    font-size: 14px;
    font-weight: 600;
}

div[data-testid="stFileUploader"] section {
    min-height: 124px;
    padding: 1rem;
    border: 1px dashed #9aa0a6;
    border-radius: 8px;
    background: var(--nm-surface);
}

div[data-testid="stFileUploader"] section:hover {
    border-color: var(--nm-blue);
    background: #f8fbff;
}

div[data-testid="stFileUploader"] button {
    min-height: 36px;
    border: 1px solid var(--nm-blue);
    border-radius: 4px;
    background: var(--nm-surface);
    color: var(--nm-blue);
    font-weight: 600;
}

div[data-testid="stFileUploader"] button:hover,
div[data-testid="stFileUploader"] button:focus {
    border-color: var(--nm-blue-hover);
    background: #f4f8fe;
    color: var(--nm-blue-hover);
}

div.stButton > button {
    width: 100%;
    min-height: 40px;
    border: 1px solid var(--nm-blue);
    border-radius: 4px;
    background: var(--nm-blue);
    color: #ffffff;
    font-weight: 600;
    letter-spacing: 0;
}

div.stButton > button:hover,
div.stButton > button:focus {
    border-color: var(--nm-blue-hover);
    background: var(--nm-blue-hover);
    color: #ffffff;
}

div.stButton > button:disabled {
    border-color: var(--nm-border);
    background: #e8eaed;
    color: #80868b;
}

.nm-file-note {
    min-height: 40px;
    display: flex;
    align-items: center;
    color: var(--nm-muted);
    font-size: 13px;
}

.nm-success {
    margin: 2rem 0 1rem;
    padding: 0.8rem 1rem;
    border-left: 4px solid var(--nm-success);
    border-radius: 4px;
    background: var(--nm-success-bg);
    color: #0d652d;
    font-size: 14px;
    font-weight: 500;
}

div[data-testid="metric-container"] {
    min-height: 96px;
    padding: 1rem;
    border: 1px solid var(--nm-border);
    border-radius: 8px;
    background: var(--nm-surface);
}

div[data-testid="metric-container"] label {
    color: var(--nm-muted);
    font-size: 12px;
}

div[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: var(--nm-text);
    font-size: 24px;
}

div[data-baseweb="tab-list"] {
    gap: 1.5rem;
    margin-top: 1.75rem;
    border-bottom: 1px solid var(--nm-border);
}

button[data-baseweb="tab"] {
    padding-right: 0;
    padding-left: 0;
    color: var(--nm-muted);
}

button[data-baseweb="tab"][aria-selected="true"] {
    color: var(--nm-blue);
}

.nm-table-wrap {
    margin-top: 1rem;
    overflow-x: auto;
    border: 1px solid var(--nm-border);
    border-radius: 8px;
    background: var(--nm-surface);
}

.nm-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}

.nm-table th {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--nm-border);
    background: #f1f3f4;
    color: var(--nm-muted);
    font-size: 12px;
    font-weight: 600;
    text-align: left;
}

.nm-table td {
    padding: 0.8rem 1rem;
    border-bottom: 1px solid #eceff1;
    color: var(--nm-text);
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
    min-height: 360px;
    margin-top: 0.75rem;
    padding: 1.5rem;
    border: 1px solid var(--nm-border);
    border-radius: 8px;
    background: var(--nm-surface);
}

.nm-reader h2 {
    margin: 0 0 1rem;
    color: var(--nm-text);
    font-size: 20px;
    font-weight: 500;
    letter-spacing: 0;
}

.nm-reader-body {
    color: #303134;
    font-family: "Noto Serif SC", "Songti SC", SimSun, serif;
    font-size: 16px;
    line-height: 1.9;
    white-space: pre-wrap;
    word-break: break-word;
}

@media (max-width: 700px) {
    .block-container {
        padding: 1rem 1rem 3rem;
    }

    .nm-topbar {
        align-items: flex-start;
    }

    .nm-brand-area {
        display: none;
    }

    .nm-page-heading {
        margin-top: 1.5rem;
    }

    .nm-page-heading h1 {
        font-size: 24px;
    }

    div[data-testid="column"] {
        width: 100% !important;
        flex: 1 1 100% !important;
    }

    .nm-reader {
        padding: 1rem;
    }
}
</style>
"""
