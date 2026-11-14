-- Test fixture extracted from n8n-mcp 2.96.2 seed schema.
CREATE TABLE nodes (
  node_type TEXT PRIMARY KEY,
  package_name TEXT NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT,
  category TEXT,
  development_style TEXT CHECK(development_style IN ('declarative', 'programmatic')),
  is_ai_tool INTEGER DEFAULT 0,
  is_trigger INTEGER DEFAULT 0,
  is_webhook INTEGER DEFAULT 0,
  is_versioned INTEGER DEFAULT 0,
  version TEXT,
  documentation TEXT,
  properties_schema TEXT,
  operations TEXT,
  credentials_required TEXT,
  outputs TEXT, -- JSON array of output definitions
  output_names TEXT, -- JSON array of output names
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
, is_tool_variant INTEGER DEFAULT 0, has_tool_variant INTEGER DEFAULT 0, base_node_type TEXT, tool_variant_of TEXT, is_community INTEGER DEFAULT 0, is_verified INTEGER DEFAULT 0, author_name TEXT, author_github_url TEXT, npm_package_name TEXT, npm_version TEXT, npm_downloads INTEGER DEFAULT 0, community_fetched_at DATETIME, npm_readme TEXT, ai_documentation_summary TEXT, ai_summary_generated_at DATETIME, deprecated_versions TEXT);

CREATE TABLE node_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_type TEXT NOT NULL,                -- e.g., "n8n-nodes-base.executeWorkflow"
  version TEXT NOT NULL,                  -- e.g., "1.0", "1.1", "2.0"
  package_name TEXT NOT NULL,             -- e.g., "n8n-nodes-base"
  display_name TEXT NOT NULL,
  description TEXT,
  category TEXT,
  is_current_max INTEGER DEFAULT 0,      -- 1 if this is the latest version
  properties_schema TEXT,                 -- JSON schema for this specific version
  operations TEXT,                        -- JSON array of operations for this version
  credentials_required TEXT,              -- JSON array of required credentials
  outputs TEXT,                           -- JSON array of output definitions
  minimum_n8n_version TEXT,               -- Minimum n8n version required (e.g., "1.0.0")
  breaking_changes TEXT,                  -- JSON array of breaking changes from previous version
  deprecated_properties TEXT,             -- JSON array of removed/deprecated properties
  added_properties TEXT,                  -- JSON array of newly added properties
  released_at DATETIME,                   -- When this version was released
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(node_type, version),
  FOREIGN KEY (node_type) REFERENCES nodes(node_type) ON DELETE CASCADE
);

CREATE TABLE templates (
  id INTEGER PRIMARY KEY,
  workflow_id INTEGER UNIQUE NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  author_name TEXT,
  author_username TEXT,
  author_verified INTEGER DEFAULT 0,
  nodes_used TEXT, -- JSON array of node types
  workflow_json TEXT, -- Complete workflow JSON (deprecated, use workflow_json_compressed)
  workflow_json_compressed TEXT, -- Compressed workflow JSON (base64 encoded gzip)
  categories TEXT, -- JSON array of categories
  views INTEGER DEFAULT 0,
  created_at DATETIME,
  updated_at DATETIME,
  url TEXT,
  scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  metadata_json TEXT, -- Structured metadata from OpenAI (JSON)
  metadata_generated_at DATETIME -- When metadata was generated
);

CREATE TABLE template_node_configs (
  id INTEGER PRIMARY KEY,
  node_type TEXT NOT NULL,
  template_id INTEGER NOT NULL,
  template_name TEXT NOT NULL,
  template_views INTEGER DEFAULT 0,

  -- Node configuration (extracted from workflow)
  node_name TEXT,                  -- Node name in workflow (e.g., "HTTP Request")
  parameters_json TEXT NOT NULL,   -- JSON: node.parameters
  credentials_json TEXT,            -- JSON: node.credentials (if present)

  -- Pre-calculated metadata for filtering
  has_credentials INTEGER DEFAULT 0,
  has_expressions INTEGER DEFAULT 0,  -- Contains {{...}} or $json/$node
  complexity TEXT CHECK(complexity IN ('simple', 'medium', 'complex')),
  use_cases TEXT,                   -- JSON array from template.metadata.use_cases

  -- Pre-calculated ranking (1 = best, 2 = second best, etc.)
  rank INTEGER DEFAULT 0,

  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE
);

CREATE TABLE workflow_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    workflow_name TEXT NOT NULL,
    workflow_snapshot TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK(trigger IN (
      'partial_update',
      'full_update',
      'autofix'
    )),
    operations TEXT,
    fix_types TEXT,
    metadata TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(instance_id, workflow_id, version_number)
  );

CREATE VIRTUAL TABLE nodes_fts USING fts5(
  node_type,
  display_name,
  description,
  documentation,
  operations,
  content='nodes',
  content_rowid='rowid'
);

CREATE VIRTUAL TABLE templates_fts USING fts5(
              name, description, content=templates
            );
