<script>
  import { onMount, tick } from 'svelte'
  import { marked } from 'marked'
  import hljs from 'highlight.js'
  import hljsDark from 'highlight.js/styles/github-dark.css?inline'
  import hljsLight from 'highlight.js/styles/github.css?inline'

  // Inject hljs themes scoped per app theme so they don't clash
  {
    const style = document.createElement('style')
    style.textContent =
      hljsDark.replaceAll('.hljs', '[data-theme="dark"] .hljs') +
      '\n' +
      hljsLight.replaceAll('.hljs', '[data-theme="light"] .hljs')
    document.head.appendChild(style)
  }

  // Theme (persisted)
  let theme = $state(localStorage.getItem('theme') ?? 'light')
  $effect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('theme', theme)
  })

  function escapeHtml(text) {
    const div = document.createElement('div')
    div.textContent = text
    return div.innerHTML
  }

  // Syntax-highlight fenced code blocks in chat markdown
  marked.use({
    renderer: {
      code({ text, lang }) {
        const language = lang && hljs.getLanguage(lang) ? lang : null
        const html = language
          ? hljs.highlight(text, { language }).value
          : escapeHtml(text)
        return `<pre><code class="hljs">${html}</code></pre>`
      },
      html(token) {
        return escapeHtml(token.text || '')
      },
    },
  })

  function highlight(text, path) {
    const ext = path?.split('.').pop()?.toLowerCase()
    try {
      if (ext && hljs.getLanguage(ext)) {
        return hljs.highlight(text, { language: ext }).value
      }
      if (text.length > 100_000) {
        return escapeHtml(text.slice(0, 100_000)) + '\n\n…(truncated)'
      }
      return hljs.highlightAuto(text).value
    } catch {
      // fall back to escaped plain text
      return escapeHtml(text)
    }
  }

  // Chat entries: { role: 'user'|'assistant'|'system', text, thinking?, images? } | { role: 'tool', ... }
  let entries = $state([])
  let input = $state('')
  let streaming = $state(false)
  let chatEl
  let inputEl

  // Model / thinking state
  let models = $state([])
  let currentModel = $state(null)
  let thinkingLevels = $state(['off'])
  let thinkingLevel = $state('off')

  // Session list
  let sessions = $state([])
  let currentSessionPath = $state('')

  // Slash commands: client-side built-ins + pi commands (extension/skill/template)
  // fetched from /commands. Built-in TUI commands (/model, /resume, ...) are NOT
  // recognized via RPC and get intercepted/blocked in handleSlashCommand.
  const BUILTIN_COMMANDS = [
    { name: 'new', description: 'Start a new session' },
    { name: 'abort', description: 'Stop the current run' },
    { name: 'compact', description: 'Compact context (optional instructions)' },
    { name: 'name', description: 'Set session display name' },
    { name: 'export', description: 'Export session to HTML' },
  ]
  let slashCommands = $state([]) // [{ name, description, source }]
  let slashSuggestions = $derived(
    input.startsWith('/') && !input.includes(' ')
      ? [...BUILTIN_COMMANDS, ...slashCommands]
          .filter((c) => c.name.startsWith(input.slice(1)))
      : []
  )

  // Footer stats
  let stats = $state(null)

  // Pending attachments: [{ kind: 'image', name, data (base64), mimeType, url (data URL) }
  //                       | { kind: 'text', name, text }]
  let attachments = $state([])

  // File browser / viewer
  let browserPath = $state('')
  let browserParent = $state(null)
  let dirEntries = $state([])
  let selectedFile = $state(null) // { path, text, reason }
  let toolsUsedInRun = $state(false)

  async function browse(path) {
    const resp = await apiGet(`/api/list?path=${encodeURIComponent(path)}`)
    browserPath = resp.path ?? ''
    browserParent = resp.parent ?? null
    dirEntries = resp.entries ?? []
  }

  async function selectEntry(e) {
    if (e.dir) {
      selectedFile = null
      await browse(e.path)
    } else {
      selectedFile = await apiGet(`/api/file?path=${encodeURIComponent(e.path)}`)
    }
  }

  // After a run that used tools, files may have changed: reload the current
  // directory and the open file. If either vanished in the meantime, fall
  // back to the project root.
  async function refreshFiles() {
    const list = await apiGet(`/api/list?path=${encodeURIComponent(browserPath)}`)
    if (!list.entries) {
      selectedFile = null
      await browse('')
      return
    }
    browserPath = list.path ?? ''
    browserParent = list.parent ?? null
    dirEntries = list.entries
    if (selectedFile) {
      const f = await apiGet(`/api/file?path=${encodeURIComponent(selectedFile.path)}`)
      if (f.error) {
        selectedFile = null
        await browse('')
      } else {
        selectedFile = f
      }
    }
  }

  function fmtSize(s) {
    if (s == null) return ''
    if (s < 1024) return `${s} B`
    if (s < 1024 * 1024) return `${(s / 1024).toFixed(1)} KB`
    return `${(s / 1024 / 1024).toFixed(1)} MB`
  }

  function renderMarkdown(text) {
    return marked.parse(text ?? '', { async: false })
  }

  let scrollQueued = false
  async function scrollToBottom() {
    if (scrollQueued) return
    scrollQueued = true
    requestAnimationFrame(async () => {
      scrollQueued = false
      await tick()
      if (chatEl) chatEl.scrollTop = chatEl.scrollHeight
    })
  }

  function currentAssistant() {
    return entries.findLast((e) => e.role === 'assistant') ?? null
  }

  async function apiGet(url) {
    try {
      const r = await fetch(url)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('GET', url, err)
      return { success: false, error: err.message }
    }
  }

  async function apiPost(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('POST', url, err)
      return { success: false, error: err.message }
    }
  }

  async function refreshState() {
    const resp = await apiGet('/state')
    if (!resp.success) return
    currentModel = resp.data.model
    thinkingLevel = resp.data.thinkingLevel
    const levels = await apiGet('/thinking_levels')
    if (levels.success) thinkingLevels = levels.data.levels
  }

  async function refreshModels() {
    const resp = await apiGet('/models')
    if (resp.success) models = resp.data.models
  }

  async function refreshStats() {
    const resp = await apiGet('/stats')
    if (resp.success) stats = resp.data
  }

  // Debounced variant: collapses bursts of triggers (agent_end + agent_settled,
  // rapid message_end events) into a single /stats fetch.
  let statsTimer = null
  function refreshStatsDebounced() {
    if (statsTimer) return
    statsTimer = setTimeout(() => {
      statsTimer = null
      refreshStats()
    }, 300)
  }

  // Guard against out-of-order responses: only the latest request may
  // update the sessions list / current marker.
  let sessionsReq = 0
  async function refreshSessions() {
    const id = ++sessionsReq
    const resp = await apiGet('/sessions')
    if (id !== sessionsReq) return
    sessions = resp.sessions ?? []
    currentSessionPath = sessions.find((s) => s.current)?.path ?? ''
  }

  async function onModelChange(e) {
    const [provider, modelId] = e.target.value.split('::')
    const resp = await apiPost('/set_model', { provider, modelId })
    if (resp.success) {
      currentModel = resp.data
      const levels = await apiGet('/thinking_levels')
      if (levels.success) thinkingLevels = levels.data.levels
      if (!thinkingLevels.includes(thinkingLevel)) thinkingLevel = 'off'
    }
  }

  async function onThinkingChange(e) {
    const resp = await apiPost('/set_thinking', { level: e.target.value })
    if (resp.success) thinkingLevel = e.target.value
  }

  async function onSessionChange(e) {
    const path = e.target.value
    if (!path || path === currentSessionPath) return
    const resp = await apiPost('/switch_session', { path })
    if (resp.success && !resp.data?.cancelled) {
      currentSessionPath = path
      entries = []
      await loadHistory()
      await refreshState()
      await refreshStats()
      inputEl?.focus()
    }
    await refreshSessions()
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case 'agent_start':
        streaming = true
        break

      case 'message_start':
        if (ev.message?.role === 'assistant') {
          entries.push({ role: 'assistant', text: '', thinking: '' })
          scrollToBottom()
        }
        break

      case 'message_update': {
        const d = ev.assistantMessageEvent
        let a = currentAssistant()
        if (!a) {
          a = { role: 'assistant', text: '', thinking: '' }
          entries.push(a)
        }
        if (d?.type === 'text_delta') {
          a.text += d.delta
        } else if (d?.type === 'thinking_delta') {
          a.thinking += d.delta
        }
        scrollToBottom()
        break
      }

      case 'message_end':
        if (ev.message?.role === 'assistant') {
          const a = currentAssistant()
          const text = (ev.message.content ?? [])
            .filter((c) => c.type === 'text')
            .map((c) => c.text)
            .join('')
          const thinking = (ev.message.content ?? [])
            .filter((c) => c.type === 'thinking')
            .map((c) => c.thinking)
            .join('')
          if (a) {
            a.text = text
            if (thinking) a.thinking = thinking
          } else if (text || thinking) {
            entries.push({ role: 'assistant', text, thinking })
          }
          // Live token/cost update once per completed assistant message.
          refreshStatsDebounced()
          // Surface failed/aborted assistant turns
          if (ev.message.stopReason === 'error') {
            entries.push({
              role: 'system',
              text: `❌ ${ev.message.errorMessage ?? 'The assistant turn failed.'}`,
            })
          } else if (ev.message.stopReason === 'aborted') {
            entries.push({ role: 'system', text: '⏹ aborted' })
          }
        }
        scrollToBottom()
        break

      case 'auto_retry_start':
        entries.push({
          role: 'system',
          text: `⚠️ ${ev.errorMessage ?? 'transient error'} — retrying (attempt ${ev.attempt}/${ev.maxAttempts} in ${Math.round((ev.delayMs ?? 0) / 1000)}s)…`,
        })
        scrollToBottom()
        break

      case 'auto_retry_end':
        if (!ev.success) {
          entries.push({
            role: 'system',
            text: `❌ Retry failed after ${ev.attempt} attempts: ${ev.finalError ?? 'unknown error'}`,
          })
        }
        scrollToBottom()
        break

      case 'compaction_end':
        if (!ev.result && !ev.aborted) {
          entries.push({
            role: 'system',
            text: `❌ Compaction failed: ${ev.errorMessage ?? 'unknown error'}`,
          })
        }
        scrollToBottom()
        break

      case 'extension_error':
        entries.push({
          role: 'system',
          text: `❌ Extension error (${ev.extensionPath}): ${ev.error}`,
        })
        scrollToBottom()
        break

      case 'tool_execution_start':
        toolsUsedInRun = true
        entries.push({
          role: 'tool',
          name: ev.toolName,
          args: ev.args,
          output: '',
          done: false,
          isError: false,
        })
        scrollToBottom()
        break

      case 'tool_execution_update': {
        const t = entries.findLast((e) => e.role === 'tool' && !e.done)
        if (t) {
          t.output = (ev.partialResult?.content ?? [])
            .map((c) => c.text ?? '')
            .join('')
        }
        scrollToBottom()
        break
      }

      case 'tool_execution_end': {
        const t = entries.findLast((e) => e.role === 'tool' && !e.done)
        if (t) {
          t.output = (ev.result?.content ?? []).map((c) => c.text ?? '').join('')
          t.done = true
          t.isError = !!ev.isError
        }
        scrollToBottom()
        break
      }

      case 'agent_end':
      case 'agent_settled':
        streaming = false
        refreshStatsDebounced()
        refreshSessions() // pick up new session files / reordered mtimes
        if (toolsUsedInRun) {
          // Consumed on the first of agent_end/agent_settled; the second is a no-op.
          toolsUsedInRun = false
          refreshFiles()
        }
        scrollToBottom()
        // Ready for the next prompt without clicking (after the disabled
        // attribute is removed by the state update).
        tick().then(() => inputEl?.focus())
        break

      case 'extension_ui_request':
        handleUiRequest(ev).catch((err) => console.error('UI request error', err))
        break
    }
  }

  async function handleUiRequest(req) {
    let resp = { type: 'extension_ui_response', id: req.id }
    if (req.method === 'notify') {
      // empty response for one-way notify
    } else if (req.method === 'setStatus' || req.method === 'setWidget' || req.method === 'setTitle') {
      if (req.method === 'setTitle' && req.title) document.title = req.title
    } else if (req.method === 'confirm') {
      resp.confirmed = window.confirm(`${req.title ?? ''}\n${req.message ?? ''}`)
    } else if (req.method === 'select') {
      const choice = window.prompt(
        `${req.title ?? 'Choose:'}\n` + req.options.map((o, i) => `${i + 1}. ${o}`).join('\n')
      )
      const idx = parseInt(choice, 10) - 1
      if (idx >= 0 && idx < req.options.length) resp.value = req.options[idx]
      else resp.cancelled = true
    } else if (req.method === 'input' || req.method === 'editor') {
      const v = window.prompt(req.title ?? 'Input:', req.prefill ?? '')
      if (v === null) resp.cancelled = true
      else resp.value = v
    } else {
      resp.cancelled = true
    }
    await apiPost('/ui-response', resp)
  }

  function onPickImages(e) {
    for (const file of e.target.files) {
      if (file.type.startsWith('image/')) {
        const reader = new FileReader()
        reader.onload = () => {
          const dataUrl = reader.result
          attachments.push({
            kind: 'image',
            name: file.name,
            data: dataUrl.split(',')[1],
            mimeType: file.type,
            url: dataUrl,
          })
        }
        reader.readAsDataURL(file)
      } else {
        // Text-ish files: inline content into the prompt message
        const reader = new FileReader()
        reader.onload = () => {
          attachments.push({
            kind: 'text',
            name: file.name,
            text: reader.result,
          })
        }
        reader.readAsText(file)
      }
    }
    e.target.value = ''
  }

  // Intercept slash commands: built-in TUI commands don't work via RPC, so map
  // the useful ones to their RPC equivalents, pass through commands pi itself
  // recognizes (extension/skill/template), and block the rest with a hint.
  async function handleSlashCommand(message) {
    const [cmd, ...rest] = message.slice(1).split(/\s+/)
    const arg = rest.join(' ')
    switch (cmd) {
      case 'new':
        await newSession()
        return
      case 'abort':
        await abort()
        return
      case 'compact': {
        const r = await apiPost('/compact', arg ? { customInstructions: arg } : {})
        entries.push({
          role: 'system',
          text: r.success
            ? ` compacted: ${r.data?.tokensBefore ?? '?'} → ~${r.data?.estimatedTokensAfter ?? '?'} tokens`
            : `⚠️ compact failed: ${r.error ?? 'unknown error'}`,
        })
        return
      }
      case 'name': {
        if (!arg) {
          entries.push({ role: 'system', text: '⚠️ usage: /name <session name>' })
          return
        }
        const r = await apiPost('/set_session_name', { name: arg })
        if (r.success) await refreshSessions()
        else entries.push({ role: 'system', text: `⚠️ ${r.error ?? 'failed to set name'}` })
        return
      }
      case 'export': {
        const r = await apiPost('/export_html')
        entries.push({
          role: 'system',
          text: r.success ? ` exported to ${r.data?.path ?? 'file'}` : `⚠️ export failed: ${r.error ?? 'unknown error'}`,
        })
        return
      }
      default:
        if (slashCommands.some((c) => c.name === cmd)) {
          // Recognized by pi (extension/skill/template) — forward as prompt.
          entries.push({ role: 'user', text: message })
          const body = { message }
          if (streaming) body.streamingBehavior = 'steer'
          await apiPost('/prompt', body)
        } else {
          const available = [
            ...BUILTIN_COMMANDS.map((c) => '/' + c.name),
            ...slashCommands.map((c) => '/' + c.name),
          ].join(', ')
          entries.push({
            role: 'system',
            text: `⚠️ Unknown command /${cmd}. Built-in TUI commands don't work here. Available: ${available}`,
          })
        }
    }
  }

  async function sendPrompt() {
    const trimmed = input.trim()
    if (trimmed.startsWith('/')) {
      input = ''
      await handleSlashCommand(trimmed)
      scrollToBottom()
      return
    }
    const textAttachments = attachments.filter((a) => a.kind === 'text')
    const imageAttachments = attachments.filter((a) => a.kind === 'image')
    let message = input.trim()
    for (const t of textAttachments) {
      message += `${message ? '\n\n' : ''}Content of \`${t.name}\`:\n\`\`\`\n${t.text}\n\`\`\``
    }
    if (!message && imageAttachments.length === 0) return
    const images = imageAttachments.map(({ data, mimeType }) => ({ data, mimeType }))
    entries.push({
      role: 'user',
      text: message,
      images: imageAttachments.map((a) => a.url),
    })
    input = ''
    attachments = []
    scrollToBottom()
    const body = { message }
    if (images.length) body.images = images
    if (streaming) body.streamingBehavior = 'steer'
    const data = await apiPost('/prompt', body)
    if (!data.success) {
      entries.push({ role: 'assistant', text: `⚠️ ${data.error ?? 'prompt rejected'}` })
    }
  }

  async function abort() {
    await apiPost('/abort')
  }

  async function newSession() {
    await apiPost('/new_session')
    entries = []
    await refreshSessions()
    await refreshStats()
    inputEl?.focus()
  }

  async function loadHistory() {
    const resp = await apiGet('/messages')
    if (!resp.success) return
    for (const m of resp.data.messages) {
      if (m.role === 'user') {
        const parts = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content ?? [])
        const text = parts.filter((c) => c.type === 'text').map((c) => c.text).join('')
        const images = parts
          .filter((c) => c.type === 'image')
          .map((c) => `data:${c.mimeType ?? c.source?.mediaType ?? 'image/png'};base64,${c.data ?? c.source?.data}`)
        if (text || images.length) entries.push({ role: 'user', text, images })
      } else if (m.role === 'assistant') {
        const text = (m.content ?? []).filter((c) => c.type === 'text').map((c) => c.text).join('')
        const thinking = (m.content ?? []).filter((c) => c.type === 'thinking').map((c) => c.thinking).join('')
        if (text || thinking) entries.push({ role: 'assistant', text, thinking })
      } else if (m.role === 'toolResult') {
        entries.push({
          role: 'tool',
          name: m.toolName,
          args: {},
          output: (m.content ?? []).map((c) => c.text ?? '').join(''),
          done: true,
          isError: !!m.isError,
        })
      }
    }
    scrollToBottom()
  }

  async function loadCommands() {
    const r = await apiGet('/commands')
    if (r.success) slashCommands = r.data?.commands ?? []
  }

  onMount(() => {
    browse('')
    // Show the project README in the file viewer on startup, if present.
    apiGet('/api/file?path=README.md').then((r) => {
      if (r.path && !r.error) selectedFile = r
    })
    loadHistory()
    refreshState()
    refreshModels()
    refreshStats()
    refreshSessions()
    loadCommands()
    inputEl?.focus()
    const es = new EventSource('/events')
    // Retry on (re)connect: covers page loads while the server was down
    // and picks up new extensions/prompts after a server restart.
    es.onopen = () => loadCommands()
    es.onmessage = (e) => {
      try {
        handleEvent(JSON.parse(e.data))
      } catch (err) {
        console.error('event handler error', err)
      }
    }
    es.onerror = (e) => console.error('SSE error', e)
    return () => es.close()
  })

  function onKeydown(e) {
    // Tab completes to the first slash-command suggestion.
    if (e.key === 'Tab' && slashSuggestions.length) {
      e.preventDefault()
      input = '/' + slashSuggestions[0].name + ' '
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendPrompt()
    }
  }

  function fmtCost(c) {
    return c == null ? '—' : `$${c.toFixed(4)}`
  }
</script>

<main>
  <div class="body">
   <div class="chatcol">
    <div class="toolbar">
      <select bind:value={theme} title="Theme">
        <option value="dark">🌙 dark</option>
        <option value="light">☀️ light</option>
      </select>
      <select value={currentModel ? `${currentModel.provider}::${currentModel.id}` : ''} onchange={onModelChange}>
        {#each models as m}
          <option value={`${m.provider}::${m.id}`} selected={currentModel && m.provider === currentModel.provider && m.id === currentModel.id}>
            {m.name ?? m.id}
          </option>
        {/each}
      </select>
      <select value={thinkingLevel} onchange={onThinkingChange}>
        {#each thinkingLevels as l}
          <option value={l} selected={l === thinkingLevel}>{l}</option>
        {/each}
      </select>
      <select value={currentSessionPath} onchange={onSessionChange}>
        {#if !currentSessionPath}
          <option value="" disabled>sessions…</option>
        {/if}
        {#each sessions as s (s.path)}
          <option value={s.path}>
            {s.name}{s.current ? ' (current)' : ''} — {new Date(s.mtime * 1000).toLocaleString()}
          </option>
        {/each}
      </select>
      {#if streaming}
        <button onclick={abort}>Abort</button>
      {/if}
      <button onclick={newSession}>New</button>
    </div>
  <div class="chat" bind:this={chatEl}>
    {#if entries.length === 0}
      <div class="chat-empty" aria-hidden="true">
        <svg class="logo" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
          <!-- circuit traces -->
          <g stroke="currentColor" stroke-width="1.5" fill="none" opacity="0.5">
            <path d="M60 18 V34" />
            <path d="M22 44 L36 52" />
            <path d="M98 44 L84 52" />
            <path d="M34 96 L44 84" />
            <path d="M86 96 L76 84" />
          </g>
          <g fill="currentColor" opacity="0.5">
            <circle cx="60" cy="14" r="4" />
            <circle cx="18" cy="42" r="4" />
            <circle cx="102" cy="42" r="4" />
            <circle cx="31" cy="101" r="4" />
            <circle cx="89" cy="101" r="4" />
          </g>
          <!-- pi glyph -->
          <path
            d="M34 44 h52 v8 h-10 v22 c0 6 3 8 8 8 v8 c-10 0 -16 -5 -16 -14 v-24 h-8 v26 c0 6 -2 10 -8 12 l-4 -7 c3 -1 4 -3 4 -7 v-24 h-8 z"
            fill="currentColor"
          />
        </svg>
        <div class="chat-empty-text">pi agent</div>
      </div>
    {/if}
    {#each entries as entry}
      {#if entry.role === 'system'}
        <div class="msg system">{entry.text}</div>
      {:else if entry.role === 'user'}
        <div class="msg user">
          {entry.text}
          {#if entry.images?.length}
            <div class="imgs">
              {#each entry.images as src}
                <img {src} alt="attachment" />
              {/each}
            </div>
          {/if}
        </div>
      {:else if entry.role === 'assistant'}
        <div class="msg assistant">
          {#if entry.thinking}
            <details class="thinking-block" open>
              <summary>thinking</summary>
              <pre>{entry.thinking}</pre>
            </details>
          {/if}
          {@html renderMarkdown(entry.text)}
        </div>
      {:else}
        <details class="msg tool" class:error={entry.isError} class:ok={entry.done && !entry.isError} open>
          <summary>
            🔧 {entry.name}
            {#if entry.args?.command}<code>{entry.args.command}</code>{/if}
            {#if !entry.done}<span class="spinner">…</span>{/if}
            {#if entry.isError}❌{/if}
          </summary>
          {#if entry.output}<pre>{entry.output}</pre>{/if}
        </details>
      {/if}
    {/each}
    {#if streaming && !currentAssistant()}
      <div class="msg assistant thinking">thinking…</div>
    {/if}
  </div>

  {#if attachments.length}
    <div class="attachments">
      {#each attachments as a, i}
        <div class="thumb">
          {#if a.kind === 'image'}
            <img src={a.url} alt="attachment" />
          {:else}
            <div class="textfile">📄 {a.name}</div>
          {/if}
          <button onclick={() => attachments.splice(i, 1)}>×</button>
        </div>
      {/each}
    </div>
  {/if}

  {#if streaming}
    <div class="progress-bar" aria-hidden="true"></div>
  {/if}
  {#if slashSuggestions.length}
    <div class="slash-menu">
      {#each slashSuggestions as c (c.name)}
        <button class="slash-item" onmousedown={(e) => { e.preventDefault(); input = '/' + c.name + ' ' }}>
          <span class="slash-name">/{c.name}</span>
          {#if c.description}<span class="slash-desc">{c.description}</span>{/if}
        </button>
      {/each}
    </div>
  {/if}
  <footer>
    <label class="attach">
      📎
      <input type="file" multiple onchange={onPickImages} hidden />
    </label>
    <textarea
      bind:this={inputEl}
      bind:value={input}
      onkeydown={onKeydown}
      placeholder={streaming ? 'Agent is working…' : 'Prompt pi… (Enter to send, Shift+Enter for newline)'}
      rows="2"
      disabled={streaming}
    ></textarea>
    <button onclick={sendPrompt} disabled={streaming || (!input.trim() && attachments.length === 0)}>Send</button>
  </footer>

  <div class="statusbar">
    {#if stats}
      <span>tokens: {stats.tokens?.total?.toLocaleString() ?? '—'}</span>
      <span>cost: {fmtCost(stats.cost)}</span>
      {#if stats.contextUsage}
        <span>context: {stats.contextUsage.percent != null ? Math.round(stats.contextUsage.percent * 100) / 100 : '—'}% ({stats.contextUsage.tokens?.toLocaleString() ?? '—'}/{stats.contextUsage.contextWindow?.toLocaleString() ?? '—'})</span>
      {/if}
      <span>tools: {stats.toolCalls ?? 0}</span>
    {:else}
      <span>loading stats…</span>
    {/if}
  </div>
   </div>

   <aside>
    <div class="browser">
      <div class="browser-path">
        {#if browserParent !== null}
          <button class="up" onclick={() => browse(browserParent)}>← up</button>
        {/if}
        <span>/{browserPath}</span>
      </div>
      <div class="dirlist">
        {#each dirEntries as e}
          <button class="direntry" class:selected={selectedFile?.path === e.path} onclick={() => selectEntry(e)}>
            <span class="icon">{e.dir ? '📁' : '📄'}</span>
            <span class="name">{e.name}</span>
            <span class="size">{fmtSize(e.size)}</span>
          </button>
        {/each}
      </div>
    </div>
    <div class="viewer">
      {#if selectedFile}
        <div class="viewer-head">
          <span class="fname">{selectedFile.path}</span>
          <a class="dl" href={`/download/${selectedFile.path}`} download>⬇ download</a>
        </div>
        {#if selectedFile.text !== null}
          <pre class="filecontent hljs"><code>{@html highlight(selectedFile.text, selectedFile.path)}</code></pre>
        {:else}
          <div class="filecontent binary">No preview ({selectedFile.reason}) — use download.</div>
        {/if}
      {:else}
        <div class="filecontent placeholder">Select a file to preview it here.</div>
      {/if}
    </div>
   </aside>
  </div>
</main>
