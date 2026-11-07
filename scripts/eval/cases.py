"""Labelled questions for the chat's live eval, over overwatch-BE itself.

Each retrieval case is worded the way someone describes behaviour, mostly without the
names in the code, and lists the files that answer it: a hit is any result in one of them.
"""

#: (question, files that answer it)
RETRIEVAL: list[tuple[str, tuple[str, ...]]] = [
    ("where do we stop someone asking too many questions in an hour", ("app/chat/limits.py", "app/core/rate_limit.py")),
    ("strip api keys and passwords out of source code before we store it", ("app/knowledge_graph/redaction.py",)),
    ("check the six digit code someone got by email when signing in", ("app/auth/otp.py",)),
    ("what rules must a new password meet and how is it hashed", ("app/auth/passwords.py",)),
    ("which columns can a card be dragged to on the kanban", ("app/boards/workflow.py",)),
    ("turn jira's rich text into markdown and back", ("app/connectors/adf.py",)),
    ("send edits made to a page here back to confluence", ("app/connectors/doc_sync.py",)),
    ("verify that an incoming github event really came from github", ("app/github/webhooks.py",)),
    ("keep a copy of the connected repository on disk", ("app/github/checkout.py", "app/integrations/git_cli.py")),
    ("decide which files in a repo get parsed and skip huge or vendored ones", ("app/knowledge_graph/discovery.py",)),
    ("match a frontend fetch call to the backend route that serves it", ("app/knowledge_graph/link/http.py",)),
    ("get the title and picture for a pasted url so it shows as a card", ("app/link_previews/service.py",)),
    ("push live updates to everyone looking at the same board", ("app/realtime/hub.py", "app/realtime/router.py")),
    ("tell what a file really is from its first bytes so a renamed file is rejected", ("app/storage/sniff.py",)),
    ("move everything one account owns onto another account", ("app/accounts/merge.py", "app/accounts/service.py")),
    ("someone signs up to hear when we launch", ("app/waitlist/service.py", "app/waitlist/router.py")),
    ("check that a quoted sentence is still in the document it came from", ("app/decisions/quotes.py",)),
    ("the ai reads a ticket and drafts the choices the team made in it", ("app/decisions/suggest.py",)),
    ("make sure the same paid ai read never runs twice at once", ("app/decisions/leases.py",)),
    ("how many ai actions a workspace gets each month", ("app/usage/allowance.py",)),
    ("find code that does what a sentence describes", ("app/embeddings/code.py", "app/embeddings/service.py")),
    ("refuse an answer that claims a link between functions the graph doesn't have", ("app/query/grounding.py",)),
    ("what the model may look up in the code while it answers", ("app/query/tools.py",)),
    ("delete a repository's data when the last workspace disconnects it", ("app/github/service.py",)),
    ("the background work that rebuilds a repository's graph after a push", ("app/workers/jobs.py",)),
    ("work out which files need reading again since the last index", ("app/knowledge_graph/sync_plan.py", "app/knowledge_graph/build/diff.py")),
    ("find board items and pages from the command palette", ("app/search/service.py",)),
    ("put a picture into a docs page and show it through a signed link", ("app/documents/attachments.py",)),
    ("the html for invitation and sign in emails", ("app/mail/templates.py",)),
    ("which flags the refresh token cookie is set with", ("app/core/cookies.py", "app/auth/router.py")),
    ("call the language model with streaming and retry when it fails", ("app/integrations/llm.py",)),
]

#: (question, files that answer it): questions that name code, as a developer types them
CODE_RETRIEVAL: list[tuple[str, tuple[str, ...]]] = [
    ("what does open_ask_context gather in parallel", ("app/query/context.py",)),
    ("how does rank_matches order the hits", ("app/query/context.py",)),
    ("how does InMemoryKnowledgeGraphStore.search filter nodes", ("app/db/knowledge_graph_store.py",)),
    ("what does redact_lines replace", ("app/knowledge_graph/redaction.py",)),
    ("where is code_similarity_threshold read", ("app/embeddings/code.py", "app/core/config.py")),
    ("what does find_in_code return", ("app/query/source.py",)),
    ("explain app/usage/allowance.py", ("app/usage/allowance.py",)),
    ("what fields does GraphDelta carry", ("app/knowledge_graph/build/model.py",)),
    ("what does sanitize_existing_checkouts clean up", ("app/github/checkout.py",)),
    ("how does calls_written_as_text parse a reply", ("app/integrations/llm.py",)),
    # plain English with one code name in it
    ("how is the SCHEMA_STATEMENTS list applied to the database", ("app/db/knowledge_graph_store.py",)),
    ("what does `safe_markdown` strip from an answer", ("app/query/grounding.py",)),
]

#: nothing in the repository answers these; any hit is noise
NEGATIVE: list[str] = [
    "a recipe for sourdough bread",
    "who won the football world cup in 2018",
    "translate a poem into french",
    "the best hiking trails in the alps",
    "how to tune a guitar by ear",
    "symptoms of the common flu",
    "train times from lahore to islamabad",
    "a knitting pattern for a scarf",
    "how far away is the moon",
    "tips for growing tomatoes on a balcony",
]

#: answer-quality questions over overwatch-BE; the first five are the cheap subset
ANSWERS: list[str] = [
    "How does the chat stop one member from asking too many questions?",
    "What happens to a repository's stored code vectors when it is disconnected?",
    "How is a webhook from GitHub checked before it is trusted?",
    "Where do we remove credentials from code before it is embedded or stored?",
    "Which part of the code turns Jira's rich text into Markdown?",
    "How does the backend send live updates to people viewing a board?",
    "What decides which files of a repository get indexed?",
    "How is a one-time sign-in code checked?",
    "What does the frontend's Kubernetes deployment look like?",
    "Which function sends invoices to Stripe?",
]
#: questions the repository can't answer: a good reply says so
UNANSWERABLE = {ANSWERS[8], ANSWERS[9]}
