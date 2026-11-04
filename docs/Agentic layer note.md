# Added a chat assistant (“Ask the map”) to map. 
It calls your existing Valhalla, VROOM, PostGIS and Elasticsearch code directly, so none of your current features change.

## How it works

Think of the AI as a dispatcher who can’t see the map but has a phone list of your services:

The user types “Nearest hospital to my point” and has clicked a point on the map.
The backend sends the question to Claude, along with a short description of each of your services (each one is called a “tool”).
Claude replies with an instruction like “call find_nearest_facility with this point, type hospital.”
Your code runs that tool, using the same nearest_facility_pg function your Facility tab already uses.
The result goes back to Claude. Claude either asks for another tool or writes the final answer.
The browser shows the answer and draws the routes, areas and markers on the map.

## Files
File	                Status	        What it does
routing-api/agent.py	New	            The AI’s instructions, the 6 tools, the agent loop, logging and rate limiting
frontend/js/agent.js	New	            The chat panel, the point picker, and drawing results on the map
frontend/css/agent.css	New	            Panel styling, using your existing color variables
app.py	                2 lines added	Connects the agent to your FastAPI app (near the bottom)
index.html	            2 lines added	Loads agent.css and agent.js
scripts.js	            1 line added	Redraws agent layers after a basemap style switch
nginx.conf	            2 changes	    Longer timeout for the agent, plus an Elasticsearch security fix (see below)
requirements.txt	    1 line added	anthropic, the Claude SDK

## Deploy step by step

1. Get an API key. Create one at console.anthropic.com.

2. Copy the files into your project. Put the backend files in your routing-api folder. Put agent.js in js/ and agent.css in css/ next to your other frontend files. Replace nginx.conf.

3. Add these lines to the routing-api service in your docker-compose:

yaml
environment:
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
  - AGENT_LOG_FILE=/app/logs/agent_log.jsonl
volumes:
  - ./logs:/app/logs

Then put ANTHROPIC_API_KEY=sk-ant-... in your .env file. Never put the key in frontend code.

4. Rebuild and restart.

bash
docker compose build routing-api && docker compose up -d
docker compose exec nginx nginx -t        # check the nginx config
docker compose restart nginx

5. Check that it’s running. Open /api/agent/health. You should see "configured": true and the list of tools.

6. Try it. Open the map and click ✦ Ask the map. Click 📍 Pick point, click the map, then press one of the example questions.

    a. Using it
- Point references: “Here”, “my point” and "هنا" all mean the point you picked. If no point is picked, the assistant asks you to pick one instead of guessing.
- Follow-ups: questions like “now route to the second one” work, because the panel sends the previous results back with each question.
- Feedback: 👍/👎 ratings are saved to agent_log.jsonl along with every question and the tools used. Review the 👎 ones each week; they show where your data or tool descriptions need improvement.

## Without the Anthropic api key we can use a real free AI model with Ollama

Ollama runs open-source models on your own server, so there’s no API key or per-question cost. Your data also stays in-house, which helps with Saudi government clients.

1. Add Ollama to docker-compose.yml:

```yaml
  ollama:
    image: ollama/ollama
    volumes:
      - ollama:/root/.ollama
    # If the server has an NVIDIA GPU, uncomment for much faster answers:
    # deploy:
    #   resources:
    #     reservations:
    #       devices: [{ driver: nvidia, count: all, capabilities: [gpu] }]

  routing-api:
    environment:
      - AGENT_PROVIDER=openai
      - AGENT_BASE_URL=http://ollama:11434/v1
      - AGENT_MODEL=qwen2.5:7b
    # ...keep your existing settings

volumes:
  ollama:
```
2. Start it and download a model (a one-time download of about 5 GB):

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b
docker compose up -d --build routing-api
```
3. Check it’s connected. /api/agent/health should now show "provider": "openai". Ask your questions in the chat panel as before.

### Some things to know about small local models:

Pick a model that supports tool calling. qwen2.5:7b, qwen3:8b and llama3.1:8b do. Qwen also handles Arabic reasonably well.
Speed depends on hardware. On CPU only, expect 20–60 seconds per answer; a GPU brings it down to a few seconds.
Small models make more mistakes. They sometimes choose the wrong tool or format arguments badly. The tools catch bad input and return an error the model can read and retry. If quality isn’t good enough, try a larger model such as qwen2.5:14b if your server has the memory.
4. Later: switching to Claude

When you get a key from console.anthropic.com, set ANTHROPIC_API_KEY and AGENT_PROVIDER=anthropic, then restart. Nothing else changes, because all three modes use the same tools, panel and logs. A good approach is to build and test with Ollama now, then compare answer quality on the same questions before deciding.

Before going to production, set AGENT_DEBUG_TOOLS=0 to turn off the /agent/tool/... test endpoint.

