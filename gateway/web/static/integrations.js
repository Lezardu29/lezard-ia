// Guides « brancher un outil » (16.4) : exemples prêts à copier, adresse et clé remplies. Utilisés par le bloc de
// partage d'une nouvelle clé (12.6) et par la section « Brancher un outil » de l'administration.
// `routes` : routes de la gateway appelées par l'outil — tests/test_integrations.py vérifie qu'elles existent.

const sh = (s) => `'${String(s).replaceAll("'", "'\\''")}'`;          // argument shell entre apostrophes

// Réglages de Claude Code (terminal et extension VS Code) pour un modèle local servi par Lezard IA, au plus près de
// l'usage avec Opus : un seul modèle pour tous les rôles (principal, rapide, sous-agents : rien à recharger sur la
// carte graphique), fenêtre de contexte annoncée (Claude Code résume avant de la dépasser), longues attentes
// acceptées, en-tête d'attribution retiré (il change à chaque demande et empêcherait Ollama de réutiliser le début
// de la conversation déjà calculé), aucune fonction d'essai propre aux modèles d'Anthropic, rien envoyé à Anthropic.
// Essais réels (Qwen 3.8 27B, carte de 12 Go, 3,9 tokens/s) : sans API_FORCE_IDLE_TIMEOUT=0, Claude Code coupe une
// réflexion de plus de 5 minutes ; les connecteurs du compte claude.ai pèsent jusqu'à 228 000 tokens et ne se
// coupent PAS depuis ce fichier (ENABLE_CLAUDEAI_MCP_SERVERS n'y a aucun effet) : variable de Windows, étape du guide.
export const CLAUDE_MODEL = "qwen3.8:27b", CLAUDE_CONTEXT = 64000;
function claudeSettings(base, key) {
  const model = Object.fromEntries(["ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"].map((n) => [n, CLAUDE_MODEL]));
  return JSON.stringify({
    env: {
      ANTHROPIC_BASE_URL: base.replace(/\/v1$/, ""),   // Claude Code ajoute lui-même /v1/messages
      ANTHROPIC_AUTH_TOKEN: key,
      ...model,
      ANTHROPIC_DEFAULT_OPUS_MODEL_NAME: "Qwen 3.8 27B (Lezard IA)",
      ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES: "effort,thinking",
      CLAUDE_CODE_MAX_CONTEXT_TOKENS: String(CLAUDE_CONTEXT),
      CLAUDE_CODE_MAX_OUTPUT_TOKENS: "16384",
      API_TIMEOUT_MS: "3600000",
      API_FORCE_IDLE_TIMEOUT: "0",   // un modèle lent réfléchit plus de 5 minutes sans rien envoyer : ne pas couper
      CLAUDE_CODE_ATTRIBUTION_HEADER: "0",
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS: "1",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      DISABLE_TELEMETRY: "1",
    },
  }, null, 2);
}

export const GUIDES = [
  {
    // Claude Code branché directement (sans routeur) : route /v1/messages, au format d'Anthropic
    id: "claudecode", name: "Claude Code (terminal et extension VS Code)", routes: ["/v1/messages"], file: "settings.json",
    steps: ["Installez le bundle « Claude Code avec Qwen 3.8 » de la marketplace (le modèle qwen3.8:27b et ses outils).",
            "« Créer le fichier » ci-dessous, puis rangez settings.json dans le dossier .claude de votre projet "
            + "(ce projet seulement) ou dans le dossier .claude de votre dossier utilisateur (tous vos projets).",
            "Rechargez VS Code (ou relancez claude) : l'extension passe par Lezard IA et Qwen reçoit tous les outils "
            + "de Claude Code. Pour revenir à votre compte Claude, supprimez le bloc « env » du fichier.",
            "Fenêtre de contexte : le fichier annonce 64 000 tokens. Donnez la même à Ollama (variable "
            + "OLLAMA_CONTEXT_LENGTH=64000, et « Context length » à 64k dans l'application Ollama), sinon il coupe "
            + "les longues demandes en silence.",
            "Connecteurs de votre compte claude.ai (s'il en a) : leurs outils dépassent la fenêtre du modèle. Coupez-les "
            + "avec la variable de Windows ENABLE_CLAUDEAI_MCP_SERVERS=false (PowerShell : setx "
            + "ENABLE_CLAUDEAI_MCP_SERVERS false), puis rouvrez VS Code ; supprimez-la pour les retrouver."],
    code: ({ base, key }) => claudeSettings(`${base}`, `${key}`),
  },
  {
    id: "openwebui", name: "Open WebUI", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Réglages d'administration → Connexions → API OpenAI → « + » (ajouter une connexion).",
            "URL et clé ci-dessous, puis Enregistrer : les modèles de la gateway apparaissent (« auto » compris).",
            "Open WebUI dans Docker sur ce même PC : remplacez localhost par host.docker.internal."],
    code: ({ base, key }) => `URL : ${base}\nClé : ${key}\n\n# ou au lancement (Docker)\n` +
      `docker run -d -p 3000:8080 \\\n  -e OPENAI_API_BASE_URL=${base} \\\n  -e OPENAI_API_KEY=${key} \\\n` +
      `  -v open-webui:/app/backend/data ghcr.io/open-webui/open-webui:main`,
  },
  {
    id: "continue", name: "Continue (VS Code, JetBrains)", routes: ["/v1/chat/completions"],
    steps: ["Ouvrez la configuration de Continue (icône engrenage → config.yaml, ou ~/.continue/config.yaml).",
            "Ajoutez ces modèles : « auto » pour le chat et les modifications, « auto/speed » pour l'autocomplétion."],
    code: ({ base, key }) => `name: Lezard IA\nversion: 1.0.0\nschema: v1\nmodels:\n` +
      `  - name: Gateway (auto)\n    provider: openai\n    model: auto\n    apiBase: ${base}\n    apiKey: ${key}\n` +
      `    roles: [chat, edit, apply]\n  - name: Gateway (rapide)\n    provider: openai\n    model: auto/speed\n` +
      `    apiBase: ${base}\n    apiKey: ${key}\n    roles: [autocomplete]`,
  },
  {
    id: "cursor", name: "Cursor", routes: ["/v1/chat/completions"], public: true,
    steps: ["Cursor Settings → Models → section « API Keys » : collez la clé dans « OpenAI API Key ».",
            "Activez « Override OpenAI Base URL » et saisissez l'adresse ci-dessous, puis Verify.",
            "« + Add model » : auto (ou auto/speed, auto/economy), et décochez les modèles que la gateway ne sert pas.",
            "Cursor appelle la gateway depuis SES serveurs : il faut une adresse Internet (accès distant), pas localhost ni le Wi-Fi."],
    code: ({ base, key }) => `OpenAI API Key          : ${key}\nOverride OpenAI Base URL : ${base}\nModèle                  : auto`,
  },
  {
    // OpenClaw : agent qui agit sur l'ordinateur et les messageries ; Lezard IA lui sert de fournisseur de modèles
    id: "openclaw", name: "OpenClaw (assistant ordinateur et messageries)", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Créez une clé réservée à OpenClaw (Clés API virtuelles ci-dessus) : budget plafonné, sans droits d'administration. Vous pourrez la révoquer seule.",
            "Mettez la clé dans la variable d'environnement LEZARD_API_KEY (première ligne ci-dessous) : elle ne reste pas en clair dans la configuration.",
            "Collez le bloc « models » et « agents » dans ~/.openclaw/openclaw.json, puis vérifiez : openclaw models status.",
            "Sécurité : OpenClaw peut agir sur votre ordinateur et vos messageries. N'installez pas de compétences tierces de son catalogue (ClawHub) sans les lire : des compétences malveillantes y ont été signalées. Pour agir sur ce PC, la commande /pc du chat Lezard IA demande votre accord à chaque action."],
    code: ({ base, key }) => `# Windows (PowerShell) ; Linux / macOS : export LEZARD_API_KEY="${key}"
` +
      `setx LEZARD_API_KEY "${key}"

` +
      `// ~/.openclaw/openclaw.json
{
  models: {
    providers: {
      lezard: {
        baseUrl: "${base}",
` +
      `        apiKey: "\${LEZARD_API_KEY}",
        api: "openai-completions",
        timeoutSeconds: 300,
` +
      `        models: [
          { id: "auto", name: "Lezard IA (auto)", contextWindow: 32768,
` +
      `            compat: { supportsReasoningEffort: false } },
` +
      `          { id: "auto/economy", name: "Lezard IA (éco : IA locales d'abord)", contextWindow: 32768,
` +
      `            compat: { supportsReasoningEffort: false } },
        ],
      },
    },
  },
` +
      `  agents: { defaults: { model: { primary: "lezard/auto" } } },
}`,
  },
  {
    // Claude Code Router : Lezard IA devient un fournisseur de Claude Code (adresse locale 127.0.0.1:3456)
    id: "ccrouter", name: "Claude Code Router (Claude Code sur Lezard IA)", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Dans un terminal : ccr ui, puis ouvrez http://127.0.0.1:3458.",
            "Providers → Add Provider → point d'accès personnalisé, protocole « OpenAI Chat » : adresse, clé et modèles ci-dessous.",
            "Server → Start, puis Agent Config → Claude Code : choisissez lezard/auto et appliquez le profil.",
            "Lancez Claude Code avec ccr code : ses demandes passent par Lezard IA (Logs pour vérifier)."],
    code: ({ base, key }) => `Nom        : lezard\nProtocole  : OpenAI Chat\nURL        : ${base}\nClé        : ${key}\n` +
      `Modèles    : auto, auto/speed, auto/economy\n\n# puis, dans le dossier du projet\nccr code`,
  },
  {
    id: "cline", name: "Cline (VS Code)", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Panneau Cline → engrenage (Settings) → API Provider : « OpenAI Compatible ».",
            "Base URL, clé et identifiant du modèle ci-dessous, puis Done.",
            "Les modèles locaux suivent moins bien les longues consignes de Cline : préférez un modèle de code récent."],
    code: ({ base, key }) => `API Provider : OpenAI Compatible\nBase URL     : ${base}\nAPI Key      : ${key}\nModel ID     : auto`,
  },
  {
    id: "kilocode", name: "Kilo Code (VS Code)", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Panneau Kilo Code → engrenage → Providers : « OpenAI Compatible ».",
            "Base URL, clé et modèle ci-dessous, puis Save."],
    code: ({ base, key }) => `API Provider : OpenAI Compatible\nBase URL     : ${base}\nAPI Key      : ${key}\nModel        : auto`,
  },
  {
    id: "anythingllm", name: "AnythingLLM", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Réglages (clé à molette) → AI Providers → LLM : « Generic OpenAI ».",
            "Base URL, clé et nom du modèle ci-dessous, puis Save changes.",
            "Fenêtre de contexte : 32768 si vous ne savez pas ; Lezard IA choisit le modèle derrière « auto »."],
    code: ({ base, key }) => `LLM Provider         : Generic OpenAI\nBase URL             : ${base}\nAPI Key              : ${key}\n` +
      `Chat Model Name      : auto\nToken context window : 32768`,
  },
  {
    id: "jan", name: "Jan", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Settings → Model Providers → « + » (ajouter un fournisseur), nommé Lezard IA.",
            "Base URL et clé ci-dessous ; les modèles de la gateway apparaissent (sinon ajoutez « auto » à la main).",
            "Dans une conversation, choisissez Lezard IA puis le modèle auto."],
    code: ({ base, key }) => `Nom      : Lezard IA\nBase URL : ${base}\nAPI Key  : ${key}\nModèle   : auto`,
  },
  {
    id: "librechat", name: "LibreChat", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Installé depuis la marketplace de Lezard IA : déjà branché, rien à faire (/librechat le démarre).",
            "Installé autrement : ajoutez LEZARD_API_KEY à son .env (première ligne ci-dessous), collez le bloc dans "
            + "librechat.yaml, puis redémarrez LibreChat.",
            "LibreChat dans Docker sur ce même PC : remplacez localhost par host.docker.internal dans baseURL."],
    code: ({ base, key }) => `# .env\nLEZARD_API_KEY=${key}\n\n# librechat.yaml\nversion: 1.2.1\nendpoints:\n  custom:\n` +
      `    - name: "Lezard IA"\n      apiKey: "\${LEZARD_API_KEY}"\n      baseURL: "${base}"\n` +
      `      models:\n        default: ["auto", "auto/speed", "auto/economy"]\n        fetch: true\n` +
      `      titleConvo: true\n      titleModel: "auto/speed"`,
  },
  {
    id: "aider", name: "Aider (terminal)", routes: ["/v1/chat/completions"],
    steps: ["Réglez l'adresse et la clé (PowerShell ci-dessous ; Linux / macOS : export).",
            "Dans un projet Git : aider avec le modèle openai/auto. Chaque modification devient un commit."],
    code: ({ base, key }) => `setx OPENAI_API_BASE "${base}"\nsetx OPENAI_API_KEY "${key}"\n\n` +
      `# nouveau terminal, dans le projet\naider --model openai/auto`,
  },
  {
    id: "opencode", name: "OpenCode (terminal)", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Collez ce bloc dans opencode.json (dossier du projet, ou ~/.config/opencode/opencode.json pour tous).",
            "Lancez opencode, puis /models : choisissez Lezard IA → auto."],
    code: ({ base, key }) => `{\n  "$schema": "https://opencode.ai/config.json",\n  "provider": {\n    "lezard": {\n` +
      `      "npm": "@ai-sdk/openai-compatible",\n      "name": "Lezard IA",\n` +
      `      "options": { "baseURL": "${base}", "apiKey": "${key}" },\n` +
      `      "models": { "auto": { "name": "Lezard IA (auto)" }, "auto/speed": { "name": "Lezard IA (rapide)" } }\n` +
      `    }\n  }\n}`,
  },
  {
    id: "goose", name: "Goose", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Settings → Models → Configure providers → Add Custom Provider.",
            "Type « OpenAI Compatible », puis les champs ci-dessous, et Create Provider.",
            "La clé est gardée dans le trousseau de Windows, pas dans un fichier."],
    code: ({ base, key }) => `Provider Type    : OpenAI Compatible\nDisplay Name     : Lezard IA\nAPI URL          : ${base}\n` +
      `API Key          : ${key}\nAvailable Models : auto, auto/speed, auto/economy\nStreaming        : activé`,
  },
  {
    id: "chatbox", name: "Chatbox", routes: ["/v1/chat/completions"],
    steps: ["Réglages → fournisseur de modèles : « OpenAI API » (ou un fournisseur personnalisé compatible OpenAI).",
            "Adresse, clé et modèle ci-dessous, puis Enregistrer."],
    code: ({ base, key }) => `API Host : ${base}\nAPI Key  : ${key}\nModèle   : auto`,
  },
  {
    id: "qwencode", name: "Qwen Code (terminal)", routes: ["/v1/chat/completions"],
    steps: ["Réglez l'adresse, la clé et le modèle (PowerShell ci-dessous ; Linux / macOS : export).",
            "Nouveau terminal, dans le projet : qwen. Il utilise Lezard IA comme fournisseur compatible OpenAI."],
    code: ({ base, key }) => `setx OPENAI_BASE_URL "${base}"\nsetx OPENAI_API_KEY "${key}"\nsetx OPENAI_MODEL "auto"\n\n` +
      `# nouveau terminal, dans le projet\nqwen`,
  },
  {
    id: "zed", name: "Zed", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Collez le bloc dans les réglages de Zed (Ctrl+, puis « Open Settings File »).",
            "La clé ne va pas dans ce fichier : saisissez-la dans Agent Settings → Lezard IA, ou mettez-la dans "
            + "la variable LEZARD_API_KEY (première ligne ci-dessous), puis redémarrez Zed.",
            "Dans le panneau Agent, choisissez le modèle auto de Lezard IA."],
    code: ({ base, key }) => `setx LEZARD_API_KEY "${key}"\n\n// settings.json\n{\n  "language_models": {\n` +
      `    "openai_compatible": {\n      "Lezard": {\n        "api_url": "${base}",\n        "available_models": [\n` +
      `          { "name": "auto", "display_name": "Lezard IA (auto)", "max_tokens": 32768 }\n        ]\n      }\n    }\n  }\n}`,
  },
  {
    id: "cherrystudio", name: "Cherry Studio", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Réglages → Fournisseurs de modèles → « + Ajouter » : nom Lezard IA, type « OpenAI ».",
            "Clé et adresse ci-dessous, puis « Gérer » : ajoutez le modèle auto (ou ceux que la gateway liste).",
            "Dans une conversation, choisissez Lezard IA → auto."],
    code: ({ base, key }) => `Type        : OpenAI\nClé API     : ${key}\nAdresse API : ${base}\nModèle      : auto`,
  },
  {
    id: "python", name: "Python (SDK openai)", routes: ["/v1/chat/completions"],
    steps: ["pip install openai"],
    code: ({ base, key }) => `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="${key}")\n` +
      `r = client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "Bonjour"}])\n` +
      `print(r.choices[0].message.content)`,
  },
  {
    id: "javascript", name: "JavaScript / Node (SDK openai)", routes: ["/v1/chat/completions"],
    steps: ["npm install openai — clé côté serveur uniquement, jamais dans une page web publique."],
    code: ({ base, key }) => `import OpenAI from "openai";\n\nconst client = new OpenAI({ baseURL: "${base}", apiKey: "${key}" });\n` +
      `const r = await client.chat.completions.create({\n  model: "auto",\n  messages: [{ role: "user", content: "Bonjour" }],\n});\n` +
      `console.log(r.choices[0].message.content);`,
  },
  {
    id: "langchain", name: "LangChain (Python)", routes: ["/v1/chat/completions", "/v1/embeddings"],
    steps: ["pip install langchain-openai",
            "Embeddings (recherche dans des documents) : OpenAIEmbeddings avec un modèle d'embeddings servi par la gateway."],
    code: ({ base, key }) => `from langchain_openai import ChatOpenAI, OpenAIEmbeddings\n\n` +
      `llm = ChatOpenAI(base_url="${base}", api_key="${key}", model="auto")\nprint(llm.invoke("Bonjour").content)\n\n` +
      `emb = OpenAIEmbeddings(base_url="${base}", api_key="${key}", model="auto",\n` +
      `                       check_embedding_ctx_length=False)\nvecteur = emb.embed_query("Bonjour")`,
  },
  {
    id: "curl", name: "curl (test en ligne de commande)", routes: ["/v1/models", "/v1/chat/completions"],
    steps: ["Liste des modèles, puis une question : une réponse JSON confirme que tout fonctionne."],
    code: ({ base, key }) => `curl ${base}/models -H ${sh(`Authorization: Bearer ${key}`)}\n\n` +
      `curl ${base}/chat/completions \\\n  -H ${sh(`Authorization: Bearer ${key}`)} \\\n  -H "Content-Type: application/json" \\\n` +
      `  -d '{"model": "auto", "messages": [{"role": "user", "content": "Bonjour"}]}'`,
  },
];

export function guideText(guide, url, key) {
  return guide.code({ base: `${url.replace(/\/+$/, "")}/v1`, key });
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function copy(button, text) {
  const label = button.textContent;
  try { await navigator.clipboard.writeText(text); button.textContent = "Copié ✓"; }
  catch { button.textContent = "Sélectionnez et copiez"; }
  setTimeout(() => { button.textContent = label; }, 1500);
}

// Sélecteur d'outil + étapes + code, dans `root` ; renvoie update(url, key, kind) pour changer d'adresse ou de clé.
export function mountGuides(root, { initial = "openwebui" } = {}) {
  root.classList.add("guides");
  const pick = el("div", "guide-tabs");
  pick.setAttribute("role", "tablist");
  const steps = el("ol", "guide-steps");
  const warn = el("p", "share-note warn");
  warn.hidden = true;
  const head = el("div", "share-head");
  const title = el("span");
  const btn = el("button", "btn btn-ghost btn-inline", "Copier");
  btn.type = "button";
  const save = el("button", "btn btn-ghost btn-inline", "Créer le fichier");   // guides à fichier (settings.json)
  save.type = "button";
  head.append(title, save, btn);
  const pre = el("pre", "guide-code");
  const block = el("div", "share-block");
  block.append(head, pre);
  root.replaceChildren(pick, steps, warn, block);
  let current = GUIDES.find((g) => g.id === initial) || GUIDES[0], url = "", key = "", kind = "";

  const render = () => {
    for (const b of pick.children) b.setAttribute("aria-selected", String(b.dataset.id === current.id));
    steps.replaceChildren(...current.steps.map((s) => el("li", "", s)));
    title.textContent = current.name;
    save.hidden = !current.file;
    save.textContent = current.file ? `Créer ${current.file}` : "";
    pre.textContent = url ? guideText(current, url, key) : "";
    warn.hidden = !(current.public && ["local", "lan", "tailscale"].includes(kind));
    warn.textContent = "Cette adresse n'est pas joignable depuis Internet : choisissez l'adresse « Internet » "
      + "(accès distant actif) pour cet outil.";
  };
  for (const g of GUIDES) {
    const b = el("button", "guide-tab", g.name.split(" (")[0]);
    b.type = "button";
    b.dataset.id = g.id;
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => { current = g; render(); });
    pick.append(b);
  }
  btn.addEventListener("click", () => copy(btn, pre.textContent));
  save.addEventListener("click", () => {   // le fichier est fabriqué dans la page : la clé ne part nulle part
    const link = el("a");
    link.href = URL.createObjectURL(new Blob([pre.textContent + "\n"], { type: "application/json" }));
    link.download = current.file;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  });
  render();
  return (u, k, kd = "") => { url = u; key = k; kind = kd; render(); };
}
