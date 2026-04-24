Start the MDR app locally (database, API, and frontend).

## Steps

1. Check if ANTHROPIC_API_KEY is set in the current shell. If not, warn the user and ask them to export it before proceeding (needed for AI mapping suggestions).

2. Start the MDR backend containers:
```bash
cd /Users/brandondorman/GitHub/lif-core/deployments/advisor-demo-docker
docker compose up -d lif-mdr-database lif-mdr-database-restore lif-mdr-api
```

3. Wait for the MDR API to be healthy by polling `http://localhost:8012/health-check` (retry a few times with short delays).

4. Start the frontend dev server:
```bash
cd /Users/brandondorman/GitHub/lif-core/frontends/mdr-frontend
VITE_API_URL=http://localhost:8012 npm run dev
```

5. Confirm everything is running and remind the user:
   - Frontend: http://localhost:5173
   - Backend: http://localhost:8012
   - Login: `smarin_lifdemo@stateu.edu` / `changeme`
