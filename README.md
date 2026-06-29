# Intelligent Police Personnel Platform

A highly secure, strictly on-premise workforce management and recommendation system built for District Police Operations. 

This platform leverages natural language processing and constraint-based algorithms to intelligently assemble task forces, query personnel records, and manage rosters without relying on any external APIs or cloud models—ensuring 100% data confidentiality.

## 🚀 Key Features

*   **Offline "Smart Search" Engine**
    *   Query the entire personnel database using conversational language (e.g., *"Show me cyber specialists transferred from Bhavnagar"*).
    *   Fully handwritten Python NLP heuristic engine featuring custom Regex intent parsing, bilingual phonetic transliteration matching, and dynamic SQL query generation.
    *   Zero reliance on external LLMs or ML models, guaranteeing complete offline security.
*   **Algorithmic Team Builder**
    *   Dynamically recommend optimal teams for specific tasks based on rank constraints, specialized skills, and geographic availability.
    *   Analyzes personnel performance records, active disciplinary status, and current duty maps to formulate recommendations.
*   **Bilingual Localization**
    *   First-class support for English, Gujarati, and "Gujlish" text parsing to break down language barriers for regional officers.
*   **A4-Ready Native Reporting**
    *   Export team rosters and personnel queries directly to perfectly formatted A4 PDFs using optimized CSS print templates.

## 🛠 Tech Stack

*   **Backend:** Python, Flask, Flask-WTF (CSRF Protection), Flask-Babel (i18n)
*   **Database:** PostgreSQL (with advanced analytical views and complex table schemas)
*   **Frontend:** HTML5, Tailwind CSS, JavaScript (Vanilla)
*   **Architecture:** Strictly monolithic and offline-first for maximum security within isolated government networks.

## 🔒 Security Architecture
The system is explicitly designed for high-confidentiality environments:
*   No outbound network connections required during runtime.
*   Role-based access control and encrypted session management.
*   Robust CSRF protection and secure SQL parameterization.

## ⚙️ Installation & Usage

1.  **Clone the repository**
2.  **Set up the environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```
3.  **Run the ETL pipeline and Application:**
    Ensure PostgreSQL is running locally, then execute the startup script to build the schemas, run the ETL, and start the server:
    ```bash
    ./run.sh
    ```
4.  **Access:**
    Navigate to `http://127.0.0.1:5001` in your browser.
