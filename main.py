import os
import requests
import openai
from openai import OpenAI
from fastapi import FastAPI, BackgroundTasks, Header, HTTPException
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from dotenv import load_dotenv

# Load secrets from .env
load_dotenv()

app = FastAPI(title="Atlas Orchestrator")


# # Wrap the headers in ClientOptions
# opts = ClientOptions(
#     headers={
#         "apikey": os.environ.get("SUPABASE_SERVICE_KEY"),
#         "Authorization": f"Bearer {os.environ.get('SUPABASE_SERVICE_KEY')}"
#     }
# )

# # Initialize Clients
# supabase: Client = create_client(
#     os.environ.get("SUPABASE_URL"), 
#     os.environ.get("SUPABASE_ANON_KEY"),
#     options=opts
# )


url = os.environ.get("SUPABASE_URL")
# Use the JWT for the initial 'handshake' so the library doesn't crash
anon_key = os.environ.get("SUPABASE_ANON_KEY") 
service_key = os.environ.get("SUPABASE_SERVICE_KEY") # Your sb_... key

supabase: Client = create_client(url, service_key)

# 2. Directly override the headers for the underlying PostgREST client
# This bypasses the JWT '3 parts' requirement and uses the raw key
# supabase.postgrest.headers.update({
#     "apikey": service_key,
#     "Authorization": f"Bearer {service_key}"
# })


# client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Use logic to use openrouter ai to be able to use the free credits for MVP
client = OpenAI(
    base_url="https://openrouter.ai/api/v1", # <--- Points traffic away from OpenAI to OpenRouter
    api_key=os.environ.get("OPENROUTER_API_KEY"), # <--- Uses your new key
    default_headers={
        "HTTP-Referer": "http://localhost:3000", # Optional: Identifies your app for their analytics
        "X-Title": "Atlas Orchestrator" # Optional: Shows up in your OpenRouter dashboard
    }
)


def process_agent_logic(message_data: dict):
    """The core engine that routes to OpenRouter using Chat History & Trigger Checks."""
    channel_id = message_data.get('channel_id')
    message_id = message_data.get('id') # Get the ID of the message we are currently processing
    
    print(f"\n⚡ Evaluating new message in Channel {channel_id}")

    # ==========================================
    # 0. MARK INCOMING MESSAGE AS PROCESSED
    # ==========================================
    # We are reading this message now, so we mark it True so we never read it again.
    if message_id:
        supabase.table('messages').update({"is_processed": True}).eq('id', message_id).execute()

    # ==========================================
    # 1. FETCH ACTIVE AGENTS (Moved up!)
    # ==========================================
    # We fetch agents first so we know their real names for the chat history
    agents_response = supabase.table('channel_agents').select('agent_id, agents(*)').eq('channel_id', channel_id).execute()
    active_agents = [row['agents'] for row in agents_response.data]

    if not active_agents:
        print("No agents found in this channel.")
        return
        
    # Create a quick dictionary mapping ID -> Name (e.g., {'uuid': 'Architect'})
    agent_dict = {agent['id']: agent['name'] for agent in active_agents}

    # ==========================================
    # 2. FETCH CHAT HISTORY (The Sliding Window)
    # ==========================================
    history_response = supabase.table('messages')\
        .select('*')\
        .eq('channel_id', channel_id)\
        .eq('status', 'APPROVED')\
        .order('created_at', desc=True)\
        .limit(5)\
        .execute()
    
    chat_history = ""
    agent_message_count = 0  
    
    # Reverse it so oldest is first, newest is last
    for msg in reversed(history_response.data):
        if msg.get('sender_id'):
            speaker = "User"
            agent_message_count = 0 
        else:
            # FIX: Look up the real name! If not found, fallback to "Unknown Agent"
            agent_name = agent_dict.get(msg.get('agent_id'), "Unknown Agent")
            speaker = f"[{agent_name}]" 
            agent_message_count += 1 
            
        chat_history += f"{speaker}: {msg['content']}\n"

    # ==========================================
    # 3. CIRCUIT BREAKER
    # ==========================================
    if agent_message_count >= 5:
        print("🛑 CIRCUIT BREAKER TRIPPED! Agents spoke 5 times in a row.")
        supabase.table('messages').insert({
            "channel_id": channel_id,
            "content": "⚠️ *System: Circuit breaker activated to prevent infinite looping. Waiting for human input.*",
            "status": "APPROVED",
            "is_processed": True 
        }).execute()
        return 

    agent_spoke = False

    # ==========================================
    # 4. THE ROUTER LOOP
    # ==========================================
    for agent in active_agents:
        
        # Stop an agent from replying to itself
        if history_response.data and history_response.data[0].get('agent_id') == agent['id']:
            continue

        print(f"Checking if {agent['name']} should speak...")
        
        trigger_prompt = (
            f"You are evaluating if an AI agent named '{agent['name']}' should reply.\n"
            f"The agent's trigger rule is: {agent.get('trigger_prompt', 'Reply if asked')}\n\n"
            f"Recent Chat History:\n{chat_history}\n\n"
            "Based ONLY on the trigger rule and the history, should this agent reply next?\n"
            "Reply with exactly one word: YES or NO."
        )
        
        try:
            check_completion = client.chat.completions.create(
                model="openrouter/free", 
                messages=[{"role": "user", "content": trigger_prompt}]
            )
            
            should_speak = check_completion.choices[0].message.content.strip().upper()
            
            if "YES" in should_speak:
                print(f"✅ {agent['name']} decided to speak.")
                agent_spoke = True
                reply_content = ""
                
                if agent['type'] == 'WEBHOOK':
                    payload = {"message": chat_history, "channel_id": channel_id, "agent_id": agent['id']}
                    res = requests.post(agent.get('webhook_url'), json=payload, headers=agent.get('webhook_headers', {}), timeout=10)
                    reply_content = res.json().get("response", "Error") if res.status_code == 200 else f"Error: {res.status_code}"
                else:
                    base_prompt = agent.get('system_prompt', "You are a helpful assistant.")
                    formatting_rule = "\n\nIMPORTANT FORMATTING RULES:\n1. Be concise.\n2. Use Markdown.\n3. No walls of text."
                    
                    response_completion = client.chat.completions.create(
                        model="openrouter/free",
                        messages=[
                            {"role": "system", "content": base_prompt + formatting_rule},
                            {"role": "user", "content": f"Chat History:\n{chat_history}"} 
                        ]
                    )
                    reply_content = response_completion.choices[0].message.content

                # --- GATEKEEPER LOGIC ---
                final_status = "APPROVED" 
                
                if agent.get('requires_approval', False):
                    if "[BLOCK]" in reply_content.upper():
                        final_status = "PENDING"
                        print(f"🚨 {agent['name']} raised a [BLOCK] flag! Setting to PENDING.")
                        reply_content = reply_content.replace("[BLOCK]", "").replace("[block]", "").strip()
                    else:
                        print(f"✅ {agent['name']} requires approval but found no issues. Auto-approving.")
                        final_status = "APPROVED"
                
                # We are generating a NEW message. If it is approved, we want the webhook to see it!
                # Therefore, is_processed must be False so the loop continues.
                is_processed_flag = True if final_status == "PENDING" else False
                
                supabase.table('messages').insert({
                    "channel_id": channel_id,
                    "content": reply_content,
                    "agent_id": agent['id'],
                    "status": final_status,
                    "is_processed": is_processed_flag 
                }).execute()
                
                break 
                
            else:
                print(f"❌ {agent['name']} stayed quiet.")
                
        except Exception as e:
            print(f"ERROR executing agent {agent['name']}: {e}")

    # ==========================================
    # 5. THE CLOSING MESSAGE
    # ==========================================
    # if not agent_spoke:
    #     print("🏁 No agents decided to speak. Conversation concluded.")
    #     last_message = history_response.data[0].get('content', '') if history_response.data else ""
        
    #     if "✅ Process completed." not in last_message:
    #         supabase.table('messages').insert({
    #             "channel_id": channel_id,
    #             "content": "✅ **Process completed.** Waiting for new instructions.",
    #             "status": "APPROVED",
    #             "is_processed": True 
    #         }).execute()

@app.post("/webhook/messages")
async def messages_webhook(
    payload: dict, 
    background_tasks: BackgroundTasks,
    authorization: str = Header(None)
):
    """Receives ping from Supabase and processes in the background."""
    expected_secret = f"Bearer {os.environ.get('WEBHOOK_SECRET')}"
    if authorization != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    message = payload.get('record', {})
    
    # --- ADDED DEBUG LOGS ---
    print(f"📥 Webhook received! Action: {payload.get('type')} | Message ID: {message.get('id')}")
    print(f"   Status: {message.get('status')} | Is_Processed: {message.get('is_processed')}")
    
    # 1. Ignore DRAFTS (Wait for human approval)
    if message.get('status') == 'PENDING':
        print("   ⛔ Ignored: Message is PENDING human approval.")
        return {"status": "Ignored - Message is pending human approval"}
        
    # 2. Ignore messages we've already processed
    if message.get('is_processed') == True:
        print("   ⛔ Ignored: Message is already processed.")
        return {"status": "Ignored - Message already processed"}
        
    print("   ✅ Accepted! Sending to Background Tasks...")
    background_tasks.add_task(process_agent_logic, message)
    return {"status": "Accepted for processing"}



def process_agent_logic2(message_data: dict):
    """The core engine that routes to OpenRouter using Chat History & Trigger Checks."""
    channel_id = message_data.get('channel_id')
    
    print(f"\n⚡ Evaluating new message in Channel {channel_id}")

    # ==========================================
    # 1. FETCH CHAT HISTORY (The Sliding Window)
    # ==========================================
    # We read from the messages table here! limit(5) prevents context bloat.
    history_response = supabase.table('messages')\
        .select('*')\
        .eq('channel_id', channel_id)\
        .eq('status', 'APPROVED')\
        .order('created_at', desc=True)\
        .limit(5)\
        .execute()
    
    chat_history = ""
    agent_message_count = 0  
    
    # Reverse it so oldest is first, newest is last for the AI to read naturally
    for msg in reversed(history_response.data):
        if msg.get('sender_id'):
            speaker = "User"
            agent_message_count = 0 # Reset loop counter if human speaks
        else:
            speaker = f"Agent {msg.get('agent_id')}"
            agent_message_count += 1 
            
        chat_history += f"{speaker}: {msg['content']}\n"

    # ==========================================
    # 2. CIRCUIT BREAKER (Infinite Loop Protection)
    # ==========================================
    if agent_message_count >= 5:
        print("🛑 CIRCUIT BREAKER TRIPPED! Agents spoke 5 times in a row.")
        supabase.table('messages').insert({
            "channel_id": channel_id,
            "content": "⚠️ *System: Circuit breaker activated to prevent infinite looping. Waiting for human input.*",
            "status": "APPROVED",
            "is_processed": True 
        }).execute()
        return # Kill the process

    # ==========================================
    # 3. FETCH ACTIVE AGENTS
    # ==========================================
    agents_response = supabase.table('channel_agents').select('agent_id, agents(*)').eq('channel_id', channel_id).execute()
    active_agents = [row['agents'] for row in agents_response.data]

    if not active_agents:
        print("No agents found in this channel.")
        return

    agent_spoke = False

    # ==========================================
    # 4. THE ROUTER LOOP (Who speaks next?)
    # ==========================================
    for agent in active_agents:
        
        # Stop an agent from replying to itself
        if history_response.data and history_response.data[0].get('agent_id') == agent['id']:
            continue

        print(f"Checking if {agent['name']} should speak...")
        
        # Ask OpenRouter a cheap YES/NO question
        trigger_prompt = (
            f"You are evaluating if an AI agent named '{agent['name']}' should reply.\n"
            f"The agent's trigger rule is: {agent.get('trigger_prompt', 'Reply if asked')}\n\n"
            f"Recent Chat History:\n{chat_history}\n\n"
            "Based ONLY on the trigger rule and the history, should this agent reply next?\n"
            "Reply with exactly one word: YES or NO."
        )
        
        try:
            check_completion = client.chat.completions.create(
                model="openrouter/free", 
                messages=[{"role": "user", "content": trigger_prompt}]
            )
            
            should_speak = check_completion.choices[0].message.content.strip().upper()
            
            if "YES" in should_speak:
                print(f"✅ {agent['name']} decided to speak.")
                
                # --- HOSTED AGENT OR PORTED AGENT LOGIC ---
                reply_content = ""
                
                if agent['type'] == 'WEBHOOK':
                    # Call remote agent
                    payload = {"message": chat_history, "channel_id": channel_id, "agent_id": agent['id']}
                    res = requests.post(agent.get('webhook_url'), json=payload, headers=agent.get('webhook_headers', {}), timeout=10)
                    reply_content = res.json().get("response", "Error") if res.status_code == 200 else f"Error: {res.status_code}"
                else:
                    # Call OpenRouter Hosted Agent
                    base_prompt = agent.get('system_prompt', "You are a helpful assistant.")
                    formatting_rule = "\n\nIMPORTANT FORMATTING RULES:\n1. Be concise.\n2. Use Markdown.\n3. No walls of text."
                    
                    response_completion = client.chat.completions.create(
                        model="openrouter/free",
                        messages=[
                            {"role": "system", "content": base_prompt + formatting_rule},
                            {"role": "user", "content": f"Chat History:\n{chat_history}"} # Send the history, not just the single user_content!
                        ]
                    )
                    reply_content = response_completion.choices[0].message.content

                # --- GATEKEEPER LOGIC (Upgraded for Dynamic Blocking) ---
                final_status = "APPROVED" # Default to approved
                
                if agent.get('requires_approval', False):
                    # If this agent requires approval, check if they raised a flag!
                    if "[BLOCK]" in reply_content.upper():
                        final_status = "PENDING"
                        print(f"🚨 {agent['name']} raised a [BLOCK] flag! Setting to PENDING.")
                        
                        # Optional: Clean up the UI by removing the [BLOCK] tag from the text
                        reply_content = reply_content.replace("[BLOCK]", "").replace("[block]", "").strip()
                    else:
                        print(f"✅ {agent['name']} requires approval but found no issues. Auto-approving.")
                        final_status = "APPROVED"
                
                # If it is Pending, set processed to True so the loop ignores it. 
                # If Approved, set to False so the next agent can see it!
                is_processed_flag = True if final_status == "PENDING" else False
                
                supabase.table('messages').insert({
                    "channel_id": channel_id,
                    "content": reply_content,
                    "agent_id": agent['id'],
                    "status": final_status,
                    "is_processed": is_processed_flag 
                }).execute()
                
                # We found an agent to speak! Break the loop so they don't all talk at once.
                break 
                
            else:
                print(f"❌ {agent['name']} stayed quiet.")
                
        except Exception as e:
            print(f"ERROR executing agent {agent['name']}: {e}")

    # ==========================================
    # 5. THE CLOSING MESSAGE (If no one spoke)
    # ==========================================
    if not agent_spoke:
        print("🏁 No agents decided to speak. Conversation concluded.")
        
        # Check the last message to ensure we don't spam "Process completed"
        last_message = history_response.data[0].get('content', '') if history_response.data else ""
        
        if "✅ Process completed." not in last_message:
            # Insert the system message
            supabase.table('messages').insert({
                "channel_id": channel_id,
                "content": "✅ **Process completed.** Waiting for new instructions.",
                "status": "APPROVED",
                "is_processed": True  # MUST be True so it doesn't trigger the webhook again!
            }).execute()


# deprecated code
def process_agent_logic1(message_data: dict):
    """The core engine that routes to OpenAI or Webhooks."""
    channel_id = message_data.get('channel_id')
    user_content = message_data.get('content')
    
    print(f"⚡ Processing message in Channel {channel_id}")
    print(f"⚡ Processing User content in Channel {user_content}")


    # 1. Fetch Agents assigned to this channel
    response = supabase.table('channel_agents')\
        .select('agent_id, agents(*)')\
        .eq('channel_id', channel_id)\
        .execute()

    # Select EVERYTHING from the channel_agents table
    # response = supabase.table('channel_agents').select('agent_id, agents(*)').execute()
        
    print (f"supabase tabe response: {response} ")
    active_agents = [row['agents'] for row in response.data]
    
    if not active_agents:
        print("No agents found in this channel.")
        return

    # 2. Loop through agents and execute
    for agent in active_agents:
        print(f"Executing Agent: {agent['name']} ({agent['type']})")
        
        try:
            reply_content = ""
            
            # --- PORTED AGENT (Webhook) ---
            if agent['type'] == 'WEBHOOK':
                webhook_url = agent.get('webhook_url')
                headers = agent.get('webhook_headers', {})
                
                payload = {
                    "message": user_content,
                    "channel_id": channel_id,
                    "agent_id": agent['id'],
                    "sender": "user"
                }
                
                res = requests.post(webhook_url, json=payload, headers=headers, timeout=10)
                if res.status_code == 200:
                    reply_content = res.json().get("response", "Error: No 'response' key")
                else:
                    reply_content = f"Error: Remote agent returned {res.status_code}"

            # --- HOSTED AGENT (OpenAI) ---
            else:
                base_prompt = agent.get('system_prompt', "You are a helpful assistant.")
                formatting_rule = (
                    "\n\nIMPORTANT FORMATTING RULES:\n"
                    "1. Be extremely concise. Get straight to the point.\n"
                    "2. Use Markdown bullet points for readability.\n"
                    "3. Never output large walls of text."
                )
                system_prompt = base_prompt + formatting_rule

                print(f"Executing Hosted Agent for the system_prompt: {system_prompt}")
                
                try:
                    # completion = client.chat.completions.create(
                    #     model="gpt-4o-mini",
                    #     messages=[
                    #         {"role": "system", "content": system_prompt},
                    #         {"role": "user", "content": user_content}
                    #     ]
                    # )
                    
                    #Using openrouter to make calls
                    # "deepseek/deepseek-r1:free" - Great for complex logic and reasoning.
                    # "google/gemini-2.0-flash-exp:free" - Lightning fast and handles massive amounts of chat history.
                    # "meta-llama/llama-3.3-70b-instruct:free" - Excellent general-purpose model.
                    completion = client.chat.completions.create(
                        # model="deepseek/deepseek-r1:free", # <--- CHANGED! (or use "google/gemini-2.0-flash-exp:free")
                        model="openrouter/free",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content}
                        ]
                    )
                    reply_content = completion.choices[0].message.content
                
                except openai.RateLimitError:
                    # Specific catch for quota/rate limits
                    reply_content = "⚠️ OpenAI Quota reached. Please check your billing/limits or try again in a few minutes."
                    print(f"CRITICAL: {reply_content}")
                
                except Exception as e:
                    # Catch-all for other AI errors (connection, etc.)
                    reply_content = f"⚠️ Agent Error: {str(e)}"
                    print(f"ERROR: {reply_content}")

            # 3. The Gatekeeper (Approval Logic)
            final_status = "PENDING" if agent.get('requires_approval', False) else "APPROVED"
            if final_status == "PENDING":
                print(f"✋ Agent {agent['name']} requires approval. Saving as DRAFT.")
            
            # 4. Save response to Supabase
            print("--- SUPABASE INSERT DEBUG ---")
            print(f"""Data to Insert: {{
                'channel_id': '{channel_id}',
                'agent_id': '{agent.get('id')}',
                'status': '{final_status}',
                'is_processed': True,
                'content_preview': '{str(reply_content)[:50]}...'
            }}""")
            print("------------------------------")
            supabase.table('messages').insert({
                "channel_id": channel_id,
                "content": reply_content,
                "agent_id": agent['id'],
                "status": final_status,
                "is_processed": True
            }, returning='minimal').execute()
            
        except Exception as e:
            print(f"CRITICAL ERROR processing agent {agent['name']}: {e}")

@app.post("/webhook/messages1")
async def messages_webhook1(
    payload: dict, 
    background_tasks: BackgroundTasks,
    authorization: str = Header(None)
):
    """Receives ping from Supabase and processes in the background."""
    # Security Check
    expected_secret = f"Bearer {os.environ.get('WEBHOOK_SECRET')}"
    if authorization != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    message = payload.get('record', {})
    
    # Kill switch: Ignore messages sent by agents to prevent infinite loops
    if message.get('agent_id') is not None:
        return {"status": "Ignored - Message from agent"}
        
    # Pass to background task so we don't timeout Supabase
    background_tasks.add_task(process_agent_logic, message)
    return {"status": "Accepted for processing"}

@app.get("/")
def health_check():
    return {"status": "Atlas Backend is running locally!"}