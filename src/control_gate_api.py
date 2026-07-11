import os
import sys
import smtplib
import requests
import threading
import psycopg2
from email.mime.text import MIMEText
from email.header import Header
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SYSTEM_LOG_BUFFER = []

def emit_ui_log(message):
    """Appends an operational milestone directly to the user viewport queue."""
    global SYSTEM_LOG_BUFFER
    print(message) # Keep standard terminal output intact
    SYSTEM_LOG_BUFFER.append(message)
    # Keep the live cache clean (trim past 50 entries)
    if len(SYSTEM_LOG_BUFFER) > 50:
        SYSTEM_LOG_BUFFER.pop(0)

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base_dir, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = str(os.getenv("SMTP_USER")).lower().strip()
SMTP_PASS = os.getenv("SMTP_PASSWORD")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

app = Flask(__name__, template_folder='templates', static_folder='../static')

@app.route('/api/discovery/logs', methods=['GET'])
def fetch_live_ui_logs():
    """Exposes current status logs to the frontend UI dashboard."""
    global SYSTEM_LOG_BUFFER
    return jsonify({
        "status": "success",
        "logs": SYSTEM_LOG_BUFFER
    })

@app.route('/api/discovery/logs/clear', methods=['POST'])
def clear_ui_logs():
    """Wipes the pipeline logs clean before initiating a new discovery pass."""
    global SYSTEM_LOG_BUFFER
    SYSTEM_LOG_BUFFER.clear()
    return jsonify({"status": "success"})

@app.errorhandler(Exception)
def handle_global_exception(e):
    print(f"[SERVER CRASH ERROR] {str(e)}", file=sys.stderr)
    return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/', methods=['GET'])
def serve_dashboard():
    return render_template('dashboard.html')

@app.route('/api/trigger/discover', methods=['POST'])
def trigger_discovery_engine():
    data = request.get_json() or {}
    target_location = data.get('location')
    target_categories = data.get('categories', [])
    selected_sources = data.get('sources', ['google'])
    
    rating_min = float(data.get('rating_min', 0.0))
    max_reviews = int(data.get('max_reviews', 100))
    
    lat = data.get('lat')
    lng = data.get('lng')
    radius_km = int(data.get('radius_km', 10))

    if not target_location:
        return jsonify({"status": "error", "message": "Location context argument missing."}), 400

    def background_worker_execution_loop():
        print(f"[THREAD WORKER] Initiating multi-channel background loop for {target_location}...")
        for platform in selected_sources:
            if platform == 'google':
                from discovery_engine import run_discovery
                for industry in target_categories:
                    run_discovery(target_location, industry, rating_min, max_reviews, lat, lng, radius_km)
                    
            elif platform == 'facebook':
                from discovery_facebook import run_facebook_discovery
                for industry in target_categories:
                    run_facebook_discovery(target_location, industry)
                    
            elif platform == 'linkedin':
                print("[PIPELINE NOTICE] LinkedIn channel skipped. Testing Meta focus phase.")
                continue
                
        print("[THREAD WORKER] Complete process successfully cleared background pass.")
        emit_ui_log("Complete process successfully cleared background pass.")

    task_thread = threading.Thread(target=background_worker_execution_loop)
    task_thread.daemon = True  
    task_thread.start()

    return jsonify({
        "status": "success", 
        "message": "Multi-channel background extraction worker initiated successfully."
    }), 202

@app.route('/api/trigger/pitch-single/<int:lead_id>', methods=['POST'])
def trigger_single_pitch_generation(lead_id):
    if not OPENAI_KEY:
        return jsonify({"status": "error", "message": "OpenAI credentials missing inside environment config."}), 401

    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT business_name, category FROM prospects WHERE id = %s;", (lead_id,))
            res = cur.fetchone()
            if not res:
                return jsonify({"status": "error", "message": "Target lead record not found."}), 404
            
            business_name, category = res[0], res[1]
            print(f"[AI WORKER] Extracting targeted copy parameters for industry: '{category}'...")

            from niche_prompts import get_niche_parameters
            niche_strategy = get_niche_parameters(category)

            headers = {
                "Authorization": f"Bearer {OPENAI_KEY}",
                "Content-Type": "application/json"
            }
            system_prompt = (
                "You are an elite B2B SaaS outreach copywriting specialist and full-stack engineer. "
                "Write a highly compelling, cold peer-to-peer outreach email from an agency to a business owner. "
                "Keep the tone direct, professional, and entirely free of corporate fluff or hype. "
                "CRITICAL: Do NOT write a subject line. Start drafting directly from the greeting (e.g., 'Hi [Owner's Name],'). "
                f"You must tailor the pitch around these specific industry pain points: {niche_strategy['pain_points']}. "
                f"Naturally weave in some of this specific terminology to demonstrate elite industry fluency: {niche_strategy['buzzwords']}."
            )
            user_prompt = (
                f"Write a hyper-personalized outreach script for '{business_name}', a highly-rated local business in the '{category}' niche.\n\n"
                f"Core Strategy Hook to use: {niche_strategy['hook_angle']}\n\n"
                "Requirements:\n"
                "1. Explicitly complement their strong Google Maps review history.\n"
                "2. Address how lacking a high-performance modern website strategy causes them to bleed local market share.\n"
                "3. Conclude with a low-friction call to action asking for a quick 5-minute sanity check sync.\n"
                "4. Do NOT output a subject header prefix line under any circumstances."
            )

            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.65,
                "max_tokens": 400
            }

            response = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=15)
            if response.status_code != 200:
                return jsonify({"status": "error", "message": f"OpenAI Gateway rejection: {response.text}"}), response.status_code

            ai_data = response.json()
            generated_copy = ai_data["choices"][0]["message"]["content"].strip()
            
            # Defensive clean block to ensure no sneaky 'Subject:' lines pass to the texteditor
            if "subject:" in generated_copy.lower():
                lines = generated_copy.split('\n')
                lines = [line for line in lines if not line.lower().startswith('subject:')]
                generated_copy = '\n'.join(lines).strip()

            suggested_angle_summary = f"Leveraging localized domain authority hooks with tailored focus on: {niche_strategy['hook_angle'][:70]}..."

            cur.execute(
                "UPDATE prospects SET ai_pitch_draft = %s, suggested_angle = %s, status = 'drafted' WHERE id = %s;",
                (generated_copy, suggested_angle_summary, lead_id)
            )
            conn.commit()
            
            return jsonify({"status": "success", "message": f"Custom script drafted for {business_name} successfully."})

@app.route('/api/queue', methods=['GET'])
def get_review_queue():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    search_query = request.args.get('q', '').strip() 
    offset = (page - 1) * per_page

    where_clause = "WHERE status != 'rejected'"
    query_params = []

    if search_query:
        where_clause += " AND (business_name ILIKE %s OR category ILIKE %s)"
        query_params.extend([f"%{search_query}%", f"%{search_query}%"])

    metrics_query = f"""
        SELECT 
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status IN ('discovered', 'drafted')) as pending,
            COUNT(*) FILTER (WHERE status IN ('sent', 'sent_followup_1')) as sent,
            COUNT(*) FILTER (WHERE status = 'followup_1') as followup
        FROM prospects 
        {where_clause};
    """
    
    data_query = f"""
        SELECT id, business_name, category, rating, review_count, suggested_angle, ai_pitch_draft, phone, email, formatted_address, google_place_id, screenshot_path, status
        FROM prospects 
        {where_clause}
        ORDER BY id DESC
        LIMIT %s OFFSET %s;
    """
    
    with psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(metrics_query, tuple(query_params))
            metrics = cur.fetchone()
            
            extended_params = query_params + [per_page, offset]
            cur.execute(data_query, tuple(extended_params))
            records = cur.fetchall()
            
            return jsonify({
                "status": "success",
                "current_page": page,
                "per_page": per_page,
                "leads": records,
                "metrics": {
                    "global_total": metrics['total'] if metrics else 0,
                    "global_pending": metrics['pending'] if metrics else 0,
                    "global_sent": metrics['sent'] if metrics else 0,
                    "global_followup": metrics['followup'] if metrics else 0
                }
            })

@app.route('/api/dispatch/<int:lead_id>', methods=['POST'])
def dispatch_lead_pitch(lead_id):
    data = request.get_json() or {}
    final_pitch_text = data.get('final_pitch')
    test_destination_email = data.get('destination_email')
    
    if not final_pitch_text:
        return jsonify({"status": "error", "message": "Payload generation body required."}), 400

    # Safety structural baseline check: strip out placeholder strings if user forgot to alter them
    final_pitch_text = final_pitch_text.replace("[Your Name]", "Mark Harvey")
    final_pitch_text = final_pitch_text.replace("[Your Agency Name]", "AsuraTECH Solutions")
    final_pitch_text = final_pitch_text.replace("[Your Contact Information]", "harvey@asuratechsolutions.com")

    final_recipient = test_destination_email if test_destination_email else SMTP_USER

    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT business_name, status FROM prospects WHERE id = %s;", (lead_id,))
            res = cur.fetchone()
            if not res:
                return jsonify({"status": "error", "message": "Target lead entry absent."}), 404
            
            business_name, current_status = res[0], res[1]
            
            if current_status == 'sent':
                followup_draft = (
                    f"Hi {business_name} Team,\n\n"
                    f"I dropped a quick custom breakdown video concept over to your inbox a couple of days ago regarding your local "
                    f"contracting digital visibility framework. I know things get chaotic out on job sites!\n\n"
                    f"Just wanted to see if you caught that breakdown layout. Let me know if you have five minutes to connect.\n\n"
                    f"Best,\nAsuraTECH Solutions Team"
                )
                cur.execute("UPDATE prospects SET ai_pitch_draft = %s, status = 'followup_1' WHERE id = %s;", (followup_draft, lead_id))
                conn.commit()
                return jsonify({"status": "success", "message": "Automated Follow-Up message draft constructed."})

            # Production Flag Configuration Layer: Change to False when you want to remove the safety [TEST RUN] label prefix
            IS_PRODUCTION_ENVIRONMENT = True 
            
            prefix_label = "" if IS_PRODUCTION_ENVIRONMENT else "[TEST RUN] "
            subject_header = f"{prefix_label}Quick sanity check for {business_name}" if current_status == 'followup_1' else f"{prefix_label}Strategic digital concept for {business_name}"
            next_pipeline_status = 'sent' if current_status != 'followup_1' else 'sent_followup_1'

            # --- DYNAMIC SIGNATURE HIGH FIDELITY LAYOUT INTEGRATION ---
            signature_html = """
            <br><br>
            <div>
              <table cellpadding="0" cellspacing="0" border="0" style="font-family: 'Segoe UI', Arial, sans-serif; color: #333333; line-height: 1.6; font-size: 14px;">
                <tbody>
                  <tr>
                    <td style="padding-right: 25px; text-align: center; vertical-align: top;">
                      <img src="https://lh3.googleusercontent.com/d/1drlL-FYwOEXGSpMl41xnbIgg06QV74Bv" alt="Mark Harvey" width="96" style="border-radius: 50%; display: block; margin: 0 auto 12px auto; border: 2px solid #0056b3;">
                      <img src="https://lh3.googleusercontent.com/d/1amHhkxXJCqP5xywg_66B1HhYArPOrJE7" alt="AsuraTECH Solutions Logo" width="96" style="display: block; margin: 0 auto;">
                      <br>
                    </td>
                    <td style="border-left: 2px solid #0056b3; padding-left: 25px; vertical-align: top;">
                      <h3 style="margin: 0 0 2px 0; font-size: 20px; color: #111111; font-weight: 700;">Mark Harvey</h3>
                      <p style="margin: 0; font-size: 15px; color: #0056b3; font-weight: 600;">Solutions Architect</p>
                      <p style="margin: 2px 0 12px 0; font-size: 13px; color: #666666; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;">AsuraTECH Solutions</p>
                      <table cellpadding="0" cellspacing="0" border="0" style="font-size: 13px; color: #555555; line-height: 1.8;">
                        <tbody>
                          <tr>
                            <td style="padding-right: 12px; vertical-align: middle;"><img src="https://img.icons8.com/ios-filled/16/0056b3/phone.png" style="display:block; border:0;" width="14" height="14"></td>
                            <td style="vertical-align: middle;">0995 982 2419</td>
                          </tr>
                          <tr>
                            <td style="padding-right: 12px; vertical-align: middle;"><img src="https://img.icons8.com/ios-filled/16/0056b3/new-post.png" style="display:block; border:0;" width="14" height="14"></td>
                            <td style="vertical-align: middle;">
                              <a href="mailto:harvey@asuratechsolutions.com" style="text-decoration: none; color: #333333; font-weight: 500;">harvey@asuratechsolutions.com</a>
                            </td>
                          </tr>
                          <tr>
                            <td style="padding-right: 12px; vertical-align: middle;"><img src="https://img.icons8.com/ios-filled/16/0056b3/globe.png" style="display:block; border:0;" width="14" height="14"></td>
                            <td style="vertical-align: middle;">
                              <a href="https://asuratechsolutions.com" style="text-decoration: none; color: #333333; font-weight: 500;">asuratechsolutions.com</a>
                            </td>
                          </tr>
                          <tr>
                            <td colspan="2" style="padding-top: 14px;">
                              <a href="https://facebook.com" style="text-decoration: none; margin-right: 12px; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/facebook-new.png" alt="Facebook" width="18" height="18" style="display: block; border: none;">
                              </a>
                              <a href="https://linkedin.com" style="text-decoration: none; margin-right: 12px; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/linkedin.png" alt="LinkedIn" width="18" height="18" style="display: block; border: none;">
                              </a>
                              <a href="https://x.com" style="text-decoration: none; margin-right: 12px; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/x.png" alt="X" width="18" height="18" style="display: block; border: none;">
                              </a>
                              <a href="https://instagram.com" style="text-decoration: none; display: inline-block;">
                                <img src="https://img.icons8.com/ios-filled/20/0056b3/instagram-new.png" alt="Instagram" width="18" height="18" style="display: block; border: none;">
                              </a>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            """

            # Remove absolute duplicate subject string fragments from body content dynamically if any slipped through text editor
            if "subject:" in final_pitch_text.lower():
                lines = final_pitch_text.split('\n')
                lines = [line for line in lines if not line.lower().startswith('subject:')]
                final_pitch_text = '\n'.join(lines).strip()

            html_formatted_body = final_pitch_text.replace("\n", "<br>")
            
            full_html_payload = f"""
            <html>
              <body style="font-family: Calibri, Arial, sans-serif; font-size: 15px; color: #222222; line-height: 1.5;">
                {html_formatted_body}
                {signature_html}
              </body>
            </html>
            """

            msg = MIMEText(full_html_payload, 'html', 'utf-8')
            msg['Subject'] = Header(subject_header, 'utf-8')
            msg['From'] = f'"AsuraTECH Solutions" <{SMTP_USER}>'
            msg['To'] = final_recipient

            clean_pass = SMTP_PASS.replace(" ", "").replace("-", "").strip()
            
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=12) as server:
                    server.login(SMTP_USER, clean_pass)
                    server.sendmail(SMTP_USER, [final_recipient], msg.as_string())
            else:
                with smtplib.SMTP(SMTP_HOST, 587, timeout=12) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(SMTP_USER, clean_pass)
                    server.sendmail(SMTP_USER, [final_recipient], msg.as_string())
            
            cur.execute("UPDATE prospects SET ai_pitch_draft = %s, email = %s, status = %s WHERE id = %s;", 
                        (final_pitch_text, final_recipient, next_pipeline_status, lead_id))
            conn.commit()
            
            return jsonify({"status": "success", "message": f"Pipeline step executed successfully to {final_recipient}."})

@app.route('/api/reject/<int:lead_id>', methods=['POST'])
def reject_lead(lead_id):
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prospects SET status = 'rejected' WHERE id = %s;", (lead_id,))
            conn.commit()
            return jsonify({"status": "success", "message": "Lead archived out of active queue framework."})

@app.route('/api/prospect/update-email/<int:lead_id>', methods=['POST'])
def update_prospect_email(lead_id):
    data = request.get_json() or {}
    new_email = data.get('email', '').strip()

    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE prospects SET email = %s WHERE id = %s;",
                (new_email if new_email else None, lead_id)
            )
            conn.commit()
            print(f"[DATABASE RECORD LINK] Updated contact target email address for Lead ID: #{lead_id} -> {new_email}")
            return jsonify({"status": "success", "message": "Lead profile updated successfully."})

@app.route('/favicon.ico')
def silence_favicon():
    return '', 204  

@app.route('/.well-known/appspecific/<path:filename>')
def silence_chrome_devtools(filename):
    return '', 204

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)