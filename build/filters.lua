-- Pandoc Lua filter for the ADK print manual (LaTeX output).

local function latex(s) return pandoc.RawInline("latex", s) end
local function latexb(s) return pandoc.RawBlock("latex", s) end

local function inlines_to_latex(inl)
  return pandoc.write(pandoc.Pandoc({ pandoc.Plain(inl) }), "latex"):gsub("\n$", "")
end

-- Escape text for LaTeX and allow line breaks after . _ / ( , = in code.
local esc = {
  ["\\"] = "\\textbackslash{}", ["{"] = "\\{", ["}"] = "\\}", ["$"] = "\\$",
  ["&"] = "\\&", ["#"] = "\\#", ["^"] = "\\textasciicircum{}", ["_"] = "\\_",
  ["%"] = "\\%", ["~"] = "\\textasciitilde{}",
}
local breakafter = { ["."] = true, ["_"] = true, ["/"] = true, ["("] = true, [","] = true, ["="] = true, [":"] = true }

local function code_tex(s)
  local out = {}
  for _, cp in utf8.codes(s) do
    local ch = utf8.char(cp)
    out[#out + 1] = esc[ch] or ch
    if breakafter[ch] then out[#out + 1] = "\\allowbreak{}" end
  end
  return table.concat(out)
end

function Code(el)
  return latex("\\texttt{" .. code_tex(el.text) .. "}")
end

-- Admonitions -> boxes. A box cannot hold a longtable, so a note with a
-- table becomes a ruled block instead.
local function has_table(blocks)
  local found = false
  pandoc.walk_block(pandoc.Div(blocks), { Table = function() found = true end })
  return found
end

local strong_types = { warning = true, danger = true, caution = true, important = true, error = true, bug = true }

function Div(el)
  if el.classes:includes("admonition") then
    local typ = el.classes[2] or "note"
    local title = el.attributes.title or ""
    local title_tex = ""
    if title ~= "" then
      local doc = pandoc.read(title, "markdown-smart")
      local inl = doc.blocks[1] and doc.blocks[1].content or {}
      title_tex = inlines_to_latex(inl)
    end
    local env = strong_types[typ] and "admonstrong" or "admon"
    local result = {}
    if has_table(el.content) then
      result[#result + 1] = latexb("\\begin{admonplain}{" .. title_tex .. "}")
      for _, b in ipairs(el.content) do result[#result + 1] = b end
      result[#result + 1] = latexb("\\end{admonplain}")
    else
      result[#result + 1] = latexb("\\begin{" .. env .. "}{" .. title_tex .. "}")
      for _, b in ipairs(el.content) do result[#result + 1] = b end
      result[#result + 1] = latexb("\\end{" .. env .. "}")
    end
    return result
  end
end

function Span(el)
  for _, cls in ipairs({ "adksupport", "tablabel", "nopython" }) do
    if el.classes:includes(cls) then
      local out = { latex("\\" .. cls .. "{") }
      for _, i in ipairs(el.content) do out[#out + 1] = i end
      out[#out + 1] = latex("}")
      return out
    end
  end
end

-- Code blocks: title caption; plain Verbatim when there is no language.
function CodeBlock(el)
  local blocks = {}
  local title = el.attributes.title
  if title and title ~= "" then
    blocks[#blocks + 1] = latexb("\\codetitle{" .. code_tex(title) .. "}")
    el.attributes.title = nil
  end
  if #el.classes == 0 then
    local text = el.text:gsub("\t", "    ")
    blocks[#blocks + 1] = latexb("\\begin{plaincode}\n" .. text .. "\n\\end{plaincode}")
  else
    blocks[#blocks + 1] = el
  end
  return blocks
end

-- Links to chapters in this volume: add the page number.
function Link(el)
  local t = el.target
  if t:sub(1, 4) == "#pg-" then
    local id = t:sub(2)
    return { el, latex("\\xpage{" .. id .. "}") }
  end
end

-- Figures: no floats. Keep the image where the text puts it.
function Figure(el)
  local out = { latexb("\\begin{center}") }
  for _, b in ipairs(el.content) do out[#out + 1] = b end
  local cap = pandoc.utils.stringify(el.caption.long or {})
  -- Alt text that is only a file name is not a useful caption.
  local is_filename = cap:match("^[%w%._%-]+%.%a%a%a%a?$") ~= nil
  if cap ~= "" and not is_filename then
    out[#out + 1] = latexb("\\par\\figcap{" .. inlines_to_latex(pandoc.utils.blocks_to_inlines(el.caption.long)) .. "}")
  end
  out[#out + 1] = latexb("\\end{center}")
  return out
end
