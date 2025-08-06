from dotenv import load_dotenv
import os
load_dotenv()  # Load environment variables from .env before anything else

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import openai
from supabase import create_client, Client
from datetime import datetime, timezone

# For OpenAI v1+ image API
openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def clean_web_output(raw):
    seen = set()
    unique_lines = []
    for line in raw.splitlines():
        if line.strip() not in seen:
            seen.add(line.strip())
            unique_lines.append(line)
    cleaned = "\n".join(unique_lines)
    if "<html" not in cleaned:
        head = "<!DOCTYPE html>\n<html>\n<head>\n<meta charset='UTF-8'>\n<title>Generated App</title>\n</head>"
        body = "<body>\n" + cleaned + "\n</body>\n</html>"
        cleaned = head + "\n" + body
    return cleaned.strip()

# Load environment variables
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
if not supabase_url or not supabase_key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables must be set")
supabase: Client = create_client(supabase_url, supabase_key)

app = Flask(__name__)
CORS(app)

# --- Landing Page Routes (must be after app is defined) ---
@app.route('/')
def landing_page():
    return send_from_directory("../frontend", "landingpage.html")

@app.route('/landing')
def landing_page_alias():
    return send_from_directory("../frontend", "landingpage.html")

# --- Serve Main App ---
@app.route('/index.html')
def index_page():
    return send_from_directory("../frontend", "index.html")

# --- Serve Pricing Page ---
@app.route('/plan.html')
@app.route('/plan')
def plan_page():
    return send_from_directory("../frontend", "plan.html")

# --- API: Add Waitlist ---
@app.route('/add-waitlist', methods=['POST', 'OPTIONS'])
def add_waitlist():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    email = data.get('email')
    # TODO: Save email to database or list
    return jsonify({'success': True})

# --- API: Register ---
@app.route('/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    # Real email verification using mails.so only if user does not exist
    import re
    email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    if not email or not re.match(email_regex, email):
        return jsonify({'error': 'Invalid email address.'}), 400
    # Check if user already exists
    try:
        existing = supabase.table('users').select('*').eq('email', email).execute()
        if hasattr(existing, 'data') and existing.data and len(existing.data) > 0:
            return jsonify({'error': 'This user already exists. Please try logging in.'}), 400
    except Exception as e:
        print('[REGISTER] Supabase check exception:', e)
        return jsonify({'error': 'Database error. Please try again.'}), 500
    # Only if user does not exist, check deliverability
    try:
        mails_api_key = "b9c66737-97ba-455a-b310-260b3735e96d"
        mails_url = f"https://api.mails.so/v1/validate?email={email}"
        mails_headers = {"x-mails-api-key": mails_api_key}
        mails_resp = requests.get(mails_url, headers=mails_headers, timeout=10)
        mails_data = mails_resp.json()
        print('[REGISTER] mails.so response:', mails_data)
        if not (mails_data.get('data') and mails_data['data'].get('result') == 'deliverable'):
            return jsonify({'error': 'Email address is not deliverable or does not exist.'}), 400
    except Exception as e:
        print('[REGISTER] mails.so exception:', e)
        return jsonify({'error': 'Email verification failed. Please try again later.'}), 500
    # Register user logic: save to Supabase
    try:
        result = supabase.table('users').insert({'email': email, 'password': password}).execute()
        if hasattr(result, 'error') and result.error:
            # Friendly error for duplicate email
            err = result.error
            if hasattr(err, 'code') and err.code == '23505':
                return jsonify({'error': 'This user already exists. Please try logging in.'}), 400
            # Fallback for other errors
            return jsonify({'error': str(err)}), 400
        return jsonify({'user': {'email': email}})
    except Exception as e:
        # Also check for duplicate error in exception string
        if 'duplicate key value violates unique constraint' in str(e):
            return jsonify({'error': 'This user already exists. Please try logging in.'}), 400
        return jsonify({'error': str(e)}), 500

# --- API: Login ---
@app.route('/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    # Only check email format for login, not deliverability
    import re
    email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    if not email or not re.match(email_regex, email):
        return jsonify({'error': 'Invalid email address.'}), 400
    # Login logic: check if user exists and password matches
    try:
        result = supabase.table('users').select('*').eq('email', email).execute()
        if hasattr(result, 'error') and result.error:
            return jsonify({'error': str(result.error)}), 400
        users = result.data if hasattr(result, 'data') else []
        if not users:
            return jsonify({'error': 'User does not exist. Please sign up first.'}), 400
        user = users[0]
        if user.get('password') != password:
            return jsonify({'error': 'Incorrect password.'}), 400
        return jsonify({'user': {'email': email}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- API: Chat (AI) ---
@app.route('/chat', methods=['POST', 'OPTIONS'])
def chat():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    message = data.get('message')
    tool = data.get('tool')
    email = data.get('email')
    plan = data.get('plan', 'starter')  # Default to 'starter' if not provided

    # Plan-based logic
    if plan == "starter":
        gpt_model = "gpt-3.5-turbo"
        logo_limit = 0
        use_custom = False
    elif plan == "pro":
        gpt_model = "gpt-3.5-turbo"
        logo_limit = 5
        use_custom = False
    elif plan == "elite":
        gpt_model = None
        logo_limit = -1  # unlimited
        use_custom = True
    else:
        gpt_model = "gpt-3.5-turbo"
        logo_limit = 0
        use_custom = False

    if tool == "logo":
        # Plan-based logo API access
        if plan == "starter":
            return jsonify({'error': 'Logo generation is not available on the starter plan.'}), 403
        elif plan == "pro":
            # Here you would check the user's usage count (not implemented)
            # For now, just simulate the limit
            # TODO: Implement real usage tracking per user
            pass  # Allow up to 5 per month
        elif plan == "elite":
            pass  # Unlimited
        # Use OpenAI DALL·E to generate a logo image
        try:
            if not openai.api_key:
                print("[ERROR] OPENAI_API_KEY is not set.")
                return jsonify({'error': 'OpenAI API key is missing on the server.'}), 500
            import re
            brand_name = None
            business_type = None
            m = re.search(r'for a ([\w\s-]+?) (brand|business|company|store|startup) called ([\w\s-]+)', message, re.IGNORECASE)
            if m:
                business_type = m.group(1).strip()
                brand_name = m.group(3).strip()
            else:
                m2 = re.search(r'called ([\w\s-]+)', message, re.IGNORECASE)
                if m2:
                    brand_name = m2.group(1).strip()
            if not brand_name:
                brand_name = "Your Brand"
            if not business_type:
                business_type = "business"
            dalle_prompt = (
                f"Logo for a {business_type} named '{brand_name}'. "
                f"Clean, modern, abstract, unique, and memorable. "
                f"Reflects the brand's personality and industry. No text or letters. Symbolism, color, and white or transparent background."
            )
            response = openai_client.images.generate(
                model="dall-e-3",
                prompt=dalle_prompt,
                n=1,
                size="1024x1024"
            )
            image_url = response.data[0].url
            return jsonify({'image': image_url})
        except Exception as e:
            import traceback
            print("[DALL·E ERROR]", traceback.format_exc())
            return jsonify({'error': f'Logo generation failed: {str(e)}'}), 500
    # Integrate with OpenAI Chat for all other tools
    try:
        if not openai.api_key:
            return jsonify({'error': 'OpenAI API key is missing on the server.'}), 500
        # Use GPT-3.5/4 for actionable, step-by-step, business-focused output
        # Use OpenAI v1+ API
        # Tool-specific system prompt for Idea Tester
        if tool == "ideatester":
            system_prompt = (
                "You are VisionFlow, an expert startup analyst and business strategist. "
                "A user will give you a business idea. Analyze the idea for: "
                "- Market saturation (is it unsaturated or crowded?)\n"
                "- Smartness and innovation\n"
                "- Potential to make someone rich (scalability, profitability)\n"
                "- Major risks or red flags\n"
                "- What would make the idea more unique or successful\n"
                "- Give a clear verdict: is this a good idea to pursue?\n"
                "- Give actionable, honest, and practical feedback.\n"
                "- Use bullet points and be specific."
            )
        else:
            system_prompt = (
                "You are VisionFlow, an expert AI business cofounder, startup advisor, and product strategist. "
                "For every answer: "
                "- Give clear, actionable, step-by-step instructions or checklists. "
                "- Use bullet points or numbered lists for clarity. "
                "- Provide practical examples or templates if possible. "
                "- Avoid generic or vague advice; be specific and tailored to the user's context. "
                "- If the user asks for a plan, provide a detailed, phase-by-phase roadmap. "
                "- If the user asks for creative ideas, give unique, original, and realistic suggestions. "
                "- Always explain the reasoning behind your advice."
            )
        if use_custom:
            # Placeholder for custom AI logic
            reply = "[Custom AI response for elite plan]"
        else:
            chat_response = openai_client.chat.completions.create(
                model=gpt_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                max_tokens=900,
                temperature=0.7
            )
            reply = chat_response.choices[0].message.content
        return jsonify({'reply': reply})
    except Exception as e:
        import traceback
        print("[AI ERROR]", traceback.format_exc())
        return jsonify({'error': f'AI error: {str(e)}', 'trace': traceback.format_exc()}), 500

import requests

# --- API: Save Favorite ---
@app.route('/save-favorite', methods=['POST', 'OPTIONS'])
def save_favorite():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    question = data.get('question')
    answer = data.get('answer')
    email = data.get('email')
    try:
        result = supabase.table('favorites').insert({'email': email, 'question': question, 'answer': answer}).execute()
        print('[SAVE FAVORITE RESULT]', result)
        if hasattr(result, 'error') and result.error:
            print('[SAVE FAVORITE ERROR]', result.error)
            return jsonify({'error': str(result.error)}), 400
        return jsonify({'message': 'Favorite saved successfully.'})
    except Exception as e:
        print('[SAVE FAVORITE EXCEPTION]', e)
        return jsonify({'error': str(e)}), 500

# --- API: View Favorites ---
@app.route('/favorites', methods=['POST', 'OPTIONS'])
def view_favorites():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    email = data.get('email')
    try:
        result = supabase.table('favorites').select('*').eq('email', email).execute()
        if hasattr(result, 'error') and result.error:
            return jsonify({'error': str(result.error)}), 400
        favorites = result.data if hasattr(result, 'data') else []
        return jsonify({'favorites': favorites})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- API: Delete Favorite ---
@app.route('/delete-favorite', methods=['POST', 'OPTIONS'])
def delete_favorite():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    email = data.get('email')
    question = data.get('question')
    try:
        result = supabase.table('favorites').delete().eq('email', email).eq('question', question).execute()
        if hasattr(result, 'error') and result.error:
            return jsonify({'error': str(result.error)}), 400
        return jsonify({'message': 'Favorite deleted.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- API: Clear Favorites ---
@app.route('/clear-favorites', methods=['POST', 'OPTIONS'])
def clear_favorites():
    if request.method == 'OPTIONS':
        return ('', 204)
    data = request.get_json()
    email = data.get('email')
    try:
        result = supabase.table('favorites').delete().eq('email', email).execute()
        if hasattr(result, 'error') and result.error:
            return jsonify({'error': str(result.error)}), 400
        return jsonify({'message': 'All favorites cleared.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/proxy-image')
def proxy_image():
    url = request.args.get('url')
    if not url or not url.startswith('https://'):
        return 'Invalid image URL', 400
    try:
        resp = requests.get(url, stream=True)
        resp.raise_for_status()
        return resp.raw.read(), 200, {
            'Content-Type': resp.headers.get('Content-Type', 'image/png'),
            'Content-Disposition': 'attachment; filename=logo.png',
            'Access-Control-Allow-Origin': '*'
        }
    except Exception as e:
        return f'Failed to fetch image: {str(e)}', 500
@app.route('/send-message', methods=['POST'])
def send_message():
    data = request.json
    name = data.get('name')
    email = data.get('email')
    message = data.get('message')

    print('[CONTACT US] Received:', data)
    import re
    email_regex = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    if not email or not re.match(email_regex, email):
        print('[CONTACT US] Invalid email:', email)
        return jsonify({"error": "Invalid email address."}), 400
    # Real email verification using mails.so
    try:
        mails_api_key = "b9c66737-97ba-455a-b310-260b3735e96d"
        mails_url = f"https://api.mails.so/v1/validate?email={email}"
        mails_headers = {"x-mails-api-key": mails_api_key}
        mails_resp = requests.get(mails_url, headers=mails_headers, timeout=10)
        print('[CONTACT US] mails.so status:', mails_resp.status_code)
        print('[CONTACT US] mails.so raw response:', mails_resp.text)
        mails_data = mails_resp.json()
        print('[CONTACT US] mails.so response:', mails_data)
        # Accept if result is 'deliverable'
        if not (mails_data.get('data') and mails_data['data'].get('result') == 'deliverable'):
            return jsonify({"error": f"Email address is not deliverable or does not exist. mails.so: {mails_data}"}), 400
    except Exception as e:
        print('[CONTACT US] mails.so exception:', e)
        return jsonify({"error": "Email verification failed. Please try again later."}), 500
    try:
        result = supabase.table("contact_us").insert({
            "name": name,
            "email": email,
            "message": message
        }).execute()
        print('[CONTACT US] Supabase result:', result)
        if hasattr(result, 'error') and result.error:
            print('[CONTACT US] Supabase error:', result.error)
            return jsonify({"error": str(result.error)}), 400
        if hasattr(result, 'data'):
            print('[CONTACT US] Supabase data:', result.data)
        else:
            print('[CONTACT US] No data in result')
        return jsonify({"success": True, "data": getattr(result, 'data', None)})
    except Exception as e:
        import traceback
        print('[CONTACT US] Exception:', e)
        print(traceback.format_exc())
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

if __name__ == "__main__":
    app.run(debug=True)
