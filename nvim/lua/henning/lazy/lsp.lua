local root_files = {
  '.luarc.json',
  '.luarc.jsonc',
  '.luacheckrc',
  '.stylua.toml',
  'stylua.toml',
  'selene.toml',
  'selene.yml',
  '.git',
}

return {
    "neovim/nvim-lspconfig",
    dependencies = {
        "stevearc/conform.nvim",
        "williamboman/mason.nvim",
        "williamboman/mason-lspconfig.nvim",
        "hrsh7th/cmp-nvim-lsp",
        "hrsh7th/cmp-buffer",
        "hrsh7th/cmp-path",
        "hrsh7th/cmp-cmdline",
        "hrsh7th/nvim-cmp",
        "L3MON4D3/LuaSnip",
        "saadparwaiz1/cmp_luasnip",
        "j-hui/fidget.nvim",
    },

    config = function()
        -- FORMATTERS
        require("conform").setup({
            formatters_by_ft = {
                -- Ruff: fix, format, and organize imports (uses pyproject.toml/ruff.toml if present)
                python = { "ruff_fix", "ruff_format", "ruff_organize_imports" },
            }
        })

        local cmp = require('cmp')
        local cmp_lsp = require("cmp_nvim_lsp")
        local capabilities = vim.tbl_deep_extend(
            "force",
            {},
            vim.lsp.protocol.make_client_capabilities(),
            cmp_lsp.default_capabilities()
        )

        require("fidget").setup({})
        require("mason").setup()
        require("mason-lspconfig").setup({
            ensure_installed = {
                "lua_ls",
                "rust_analyzer",
                "gopls",
                -- ⬇️ Python
                "basedpyright",
                "ruff",
            },
            handlers = {
                function(server_name) -- default handler
                    require("lspconfig")[server_name].setup {
                        capabilities = capabilities
                    }
                end,

                -- Zig
                zls = function()
                    local lspconfig = require("lspconfig")
                    lspconfig.zls.setup({
                        root_dir = lspconfig.util.root_pattern(".git", "build.zig", "zls.json"),
                        settings = {
                            zls = {
                                enable_inlay_hints = true,
                                enable_snippets = true,
                                warn_style = true,
                            },
                        },
                    })
                    vim.g.zig_fmt_parse_errors = 0
                    vim.g.zig_fmt_autosave = 0
                end,

                -- Lua
                ["lua_ls"] = function()
                    local lspconfig = require("lspconfig")
                    lspconfig.lua_ls.setup {
                        capabilities = capabilities,
                        settings = {
                            Lua = {
                                format = {
                                    enable = true,
                                    defaultConfig = {
                                        indent_style = "space",
                                        indent_size = "2",
                                    }
                                },
                            }
                        }
                    }
                end,

                -- Ruff LSP (linting/format/imports)
                ["ruff"] = function()
                    local lspconfig = require("lspconfig")
                    lspconfig.ruff.setup({
                        capabilities = capabilities,
                        init_options = {
                            settings = {
                            }
                        },
                        on_attach = function(client, bufnr)
                            client.server_capabilities.hoverProvider = false
                        end,
                        root_dir = lspconfig.util.root_pattern(
                            "pyproject.toml", "ruff.toml",
                            "setup.cfg","setup.py","requirements.txt","Pipfile",".git"
                        ),
                    })
                end,

                -- 🐍 BasedPyright (type checking & IntelliSense)
                ["basedpyright"] = function()
                    local lspconfig = require("lspconfig")
                    lspconfig.basedpyright.setup({
                        capabilities = capabilities,
                        root_dir = lspconfig.util.root_pattern(
                            "pyproject.toml","pyrightconfig.json",
                            "setup.cfg","setup.py","requirements.txt","Pipfile",".git"
                        ),
                        settings = {
                            basedpyright = {
                                -- sensible defaults; adjust to taste in your project config
                                analysis = {
                                    typeCheckingMode = "standard",
                                    autoImportCompletions = true,
                                },
                                -- Ruff organizes imports for us
                                disableOrganizeImports = true,
                            },
                        },
                    })
                end,
            }
        })

        -- CMP
        local cmp_select = { behavior = cmp.SelectBehavior.Select }
        cmp.setup({
            snippet = {
                expand = function(args)
                    require('luasnip').lsp_expand(args.body)
                end,
            },
            mapping = cmp.mapping.preset.insert({
                ['<C-p>'] = cmp.mapping.select_prev_item(cmp_select),
                ['<C-n>'] = cmp.mapping.select_next_item(cmp_select),
                ['<C-y>'] = cmp.mapping.confirm({ select = true }),
                ["<C-Space>"] = cmp.mapping.complete(),
            }),
            sources = cmp.config.sources({
                { name = "copilot", group_index = 2 },
                { name = 'nvim_lsp' },
                { name = 'luasnip' },
            }, {
                { name = 'buffer' },
            })
        })

        vim.diagnostic.config({
            float = {
                focusable = false,
                style = "minimal",
                border = "rounded",
                source = "always",
                header = "",
                prefix = "",
            },
        })
    end
}

