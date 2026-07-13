# Police Personnel Platform

An on-premise workforce management and recommendation system designed for district police operations.

This platform uses natural language processing and constraint-based algorithms to assemble task forces, query personnel records, and manage rosters. It operates entirely offline without relying on external APIs or cloud models to ensure data confidentiality.

## Key Features

*   **Smart Search Engine**
    *   Query the personnel database using conversational language in English or Gujarati.
    *   Powered by an integrated Local LLM that extracts intents and filters from natural language, routing them to dynamic SQL generators.
    *   Features a cross-lingual anti-hallucination layer mapping LLM outputs strictly to validated database tokens and aliases.
*   **Algorithmic Team Builder**
    *   Recommend teams for specific tasks based on rank constraints, specialized skills, and geographic availability.
    *   Analyzes personnel performance records, active disciplinary status, and current duty maps to generate recommendations.
    *   Includes a Shuffle mechanism to instantly swap candidates with equally qualified alternatives on the dashboard.
*   **Bilingual Localization**
    *   Support for English, Gujarati, and Gujlish text parsing to handle regional input variations.
*   **A4 Reporting**
    *   Export team rosters and personnel queries to formatted A4 PDFs using native CSS print templates.

## Tech Stack

*   **Backend:** Python, Flask, Flask-WTF, Flask-Babel
*   **Database:** PostgreSQL
*   **Frontend:** HTML5, Tailwind CSS, Vanilla JavaScript
*   **Architecture:** Monolithic and offline-first

## Security Architecture

Designed for high-confidentiality environments:
*   No outbound network connections required during runtime.
*   Role-based access control and encrypted session management.
*   CSRF protection and SQL parameterization.

## Installation & Usage

1.  **Clone the repository**
2.  **Set up the environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```
3.  **Run the ETL pipeline and application:**
    Ensure PostgreSQL is running locally, then execute the startup script to build the schemas, run the ETL, and start the server:
    ```bash
    ./run.sh
    ```
4.  **Access:**
    Navigate to `http://127.0.0.1:5001` in your browser.
