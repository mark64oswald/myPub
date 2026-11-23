"""ui_emit.py — the code ``/kb-ui`` writes from a UI spec (2026-11).

Everything here is an overlay for a project that ``npx shadcn@latest init``
scaffolds (``--template vite|next``, ``--base base|radix``): the files go
under the project's ``@/`` alias root (``src/``), next to the components
``npx shadcn add`` installs. Nothing here duplicates a shadcn component;
the ``components/kb/`` files compose them.

Two axes vary the output:

* ``base``  — Base UI and Radix compose triggers differently
  (``render={<Button/>}`` vs ``asChild``) and Base UI's Select takes
  ``items``. Only ``app-shell``, ``date-field`` and ``enum-select`` differ.
* ``stack`` — Vite gets ``App.tsx`` with hash routes; Next.js gets one
  ``app/<slug>/page.tsx`` per page. Views and components are shared.

The TanStack Table code follows the v9 API the shadcn Data Table docs use
(``tableFeatures``, ``useTable``, ``table.FlexRender``).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import date, timedelta
from typing import Any, Optional, Sequence

import ui_spec as us

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _js(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _fill(template: str, **kw: str) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace(f"__{k}__", v)
    return out


TS_TYPE = {"id": "number", "fk": "number", "integer": "number", "number": "number",
           "currency": "number", "percent": "number", "boolean": "boolean",
           "date": "string", "timestamp": "string"}


def ts_type(f: us.Field) -> str:
    if f.kind == "id" and f.physical_type.lower().startswith(("string", "varchar", "char", "uuid")):
        return "string"
    if f.kind == "fk" and f.physical_type.lower().startswith(("string", "varchar", "char", "uuid")):
        return "string"
    if f.kind == "enum" and f.enum_values:
        return " | ".join(_js(v) for v in f.enum_values)
    return TS_TYPE.get(f.kind, "string")


def col_kind(f: us.Field) -> str:
    """Formatting kind for kb-format: text | number | integer | currency | percent | date | boolean."""
    return {"id": "text", "fk": "text", "enum": "text", "longtext": "text",
            "timestamp": "date"}.get(f.kind, f.kind)


# ---------------------------------------------------------------------------
# Shared components
# ---------------------------------------------------------------------------

FORMAT_TS = '''\
// Formatting for table cells, KPI values and record fields.
export type ValueKind =
  | "text"
  | "number"
  | "integer"
  | "currency"
  | "percent"
  | "date"
  | "boolean"

const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
})
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 })
const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 })
const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
})

export function formatValue(value: unknown, kind: ValueKind = "text"): string {
  if (value === null || value === undefined || value === "") return "—"
  switch (kind) {
    case "currency":
      return currency.format(Number(value))
    case "percent":
      return `${number.format(Number(value))}%`
    case "integer":
      return integer.format(Number(value))
    case "number":
      return number.format(Number(value))
    case "boolean":
      return value ? "Yes" : "No"
    case "date": {
      const d = new Date(String(value))
      return Number.isNaN(d.getTime())
        ? String(value)
        : d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" })
    }
    default:
      return String(value)
  }
}

export function formatCompact(value: number, kind: ValueKind): string {
  if (kind === "currency") return `$${compact.format(value)}`
  if (kind === "percent") return `${number.format(value)}%`
  return compact.format(value)
}
'''

DATA_TABLE_TSX = '''\
"use client"

import * as React from "react"
import {
  createColumnHelper,
  createPaginatedRowModel,
  createSortedRowModel,
  rowPaginationFeature,
  rowSortingFeature,
  sortFn_alphanumeric,
  sortFn_text,
  tableFeatures,
  useTable,
  type SortingState,
} from "@tanstack/react-table"
import { ArrowUpDown } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { EnumSelect } from "@/components/kb/enum-select"
import { formatValue, type ValueKind } from "@/lib/kb-format"

// Sorting and paging run in TanStack Table (v9 API, as in the shadcn Data
// Table docs); the search box and facet filters narrow the rows first.
const features = tableFeatures({
  rowPaginationFeature,
  rowSortingFeature,
  paginatedRowModel: createPaginatedRowModel(),
  sortedRowModel: createSortedRowModel(),
  sortFns: { alphanumeric: sortFn_alphanumeric, text: sortFn_text },
})

export type Row = Record<string, unknown>

export type ColumnSpec = {
  key: string
  label: string
  kind: ValueKind
}

export type Facet = {
  key: string
  label: string
  options: string[]
}

const ALL = "__all__"

export function DataTable({
  data,
  columns,
  searchKey,
  searchLabel,
  facets = [],
  onRowClick,
}: {
  data: Row[]
  columns: ColumnSpec[]
  searchKey?: string
  searchLabel?: string
  facets?: Facet[]
  onRowClick?: (row: Row) => void
}) {
  const [sorting, setSorting] = React.useState<SortingState>([])
  const [search, setSearch] = React.useState("")
  const [facetValues, setFacetValues] = React.useState<Record<string, string>>({})

  const rows = React.useMemo(() => {
    const q = search.trim().toLowerCase()
    return data.filter((row) => {
      if (q && searchKey && !String(row[searchKey] ?? "").toLowerCase().includes(q)) {
        return false
      }
      return facets.every((f) => {
        const v = facetValues[f.key]
        return !v || v === ALL || String(row[f.key] ?? "") === v
      })
    })
  }, [data, search, searchKey, facets, facetValues])

  const columnHelper = createColumnHelper<typeof features, Row>()
  const tableColumns = React.useMemo(
    () =>
      columnHelper.columns(
        columns.map((c) =>
          columnHelper.accessor((row) => row[c.key] as string | number | null, {
            id: c.key,
            header: ({ column }) => (
              <Button
                variant="ghost"
                size="sm"
                className={c.kind === "text" || c.kind === "date" ? "-ml-2" : "ml-auto"}
                onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
              >
                {c.label}
                <ArrowUpDown />
              </Button>
            ),
            cell: ({ row }) => (
              <div
                className={
                  c.kind === "currency" || c.kind === "number" || c.kind === "integer" || c.kind === "percent"
                    ? "text-right tabular-nums"
                    : undefined
                }
              >
                {formatValue(row.original[c.key], c.kind)}
              </div>
            ),
          })
        )
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [columns]
  )

  const table = useTable({
    features,
    data: rows,
    columns: tableColumns,
    onSortingChange: setSorting,
    state: { sorting },
  })

  return (
    <div className="w-full">
      <div className="flex flex-wrap items-center gap-2 py-4">
        {searchKey ? (
          <Input
            placeholder={`Search ${searchLabel ?? searchKey}...`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="max-w-sm"
          />
        ) : null}
        {facets.map((f) => (
          <EnumSelect
            key={f.key}
            value={facetValues[f.key] ?? ALL}
            onChange={(v) => setFacetValues((prev) => ({ ...prev, [f.key]: v }))}
            options={[ALL, ...f.options]}
            labels={{ [ALL]: `All ${f.label.toLowerCase()}` }}
            className="w-48"
          />
        ))}
      </div>
      <div className="overflow-hidden rounded-md border">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <TableHead key={header.id}>
                    {header.isPlaceholder ? null : <table.FlexRender header={header} />}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows?.length ? (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className={onRowClick ? "cursor-pointer" : undefined}
                  onClick={() => onRowClick?.(row.original)}
                >
                  {row.getAllCells().map((cell) => (
                    <TableCell key={cell.id}>
                      <table.FlexRender cell={cell} />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell colSpan={columns.length} className="h-24 text-center">
                  No results.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      <div className="flex items-center justify-end gap-2 py-4">
        <div className="flex-1 text-sm text-muted-foreground">
          {rows.length} of {data.length} row(s)
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => table.previousPage()}
          disabled={!table.getCanPreviousPage()}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => table.nextPage()}
          disabled={!table.getCanNextPage()}
        >
          Next
        </Button>
      </div>
    </div>
  )
}
'''

KPI_CARD_TSX = '''\
"use client"

import { TrendingDown, TrendingUp } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { formatValue, type ValueKind } from "@/lib/kb-format"

// A headline number with its change against the previous period, as in the
// section-cards pattern of the shadcn dashboard-01 block.
export function KpiCard({
  label,
  value,
  previous,
  kind,
  note,
}: {
  label: string
  value: number
  previous?: number
  kind: ValueKind
  note?: string
}) {
  const delta =
    previous !== undefined && previous !== 0 ? ((value - previous) / Math.abs(previous)) * 100 : undefined
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl font-semibold tabular-nums">
          {formatValue(value, kind)}
        </CardTitle>
        {delta !== undefined ? (
          <Badge variant="outline" className="w-fit">
            {delta >= 0 ? <TrendingUp /> : <TrendingDown />}
            {delta >= 0 ? "+" : ""}
            {delta.toFixed(1)}%
          </Badge>
        ) : null}
      </CardHeader>
      {note ? (
        <CardFooter className="text-sm text-muted-foreground">{note}</CardFooter>
      ) : null}
    </Card>
  )
}
'''

TREND_CHART_TSX = '''\
"use client"

import { Area, AreaChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { formatCompact, type ValueKind } from "@/lib/kb-format"

export type Point = { period: string; value: number }

// A measure over time: an area for amounts and counts, a line for rates
// (rates don't add up, so a filled area would suggest a total).
export function TrendChart({
  title,
  description,
  data,
  kind,
  label,
}: {
  title: string
  description?: string
  data: Point[]
  kind: ValueKind
  label: string
}) {
  const config = { value: { label, color: "var(--chart-1)" } } satisfies ChartConfig
  const asLine = kind === "percent"
  const axis = (
    <>
      <CartesianGrid vertical={false} />
      <XAxis dataKey="period" tickLine={false} axisLine={false} tickMargin={8} minTickGap={24} />
      <YAxis
        tickLine={false}
        axisLine={false}
        width={56}
        tickFormatter={(v: number) => formatCompact(v, kind)}
      />
      <ChartTooltip content={<ChartTooltipContent />} />
    </>
  )
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>
        <ChartContainer config={config} className="aspect-auto h-[260px] w-full">
          {asLine ? (
            <LineChart data={data} margin={{ left: 4, right: 12 }}>
              {axis}
              <Line dataKey="value" type="monotone" stroke="var(--color-value)" strokeWidth={2} dot={false} />
            </LineChart>
          ) : (
            <AreaChart data={data} margin={{ left: 4, right: 12 }}>
              {axis}
              <Area
                dataKey="value"
                type="monotone"
                fill="var(--color-value)"
                fillOpacity={0.3}
                stroke="var(--color-value)"
              />
            </AreaChart>
          )}
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
'''

BREAKDOWN_CHART_TSX = '''\
"use client"

import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { formatCompact, type ValueKind } from "@/lib/kb-format"

export type Slice = { category: string; value: number }

// A measure across categories: horizontal bars, largest first, so long
// category names stay readable and the ranking reads top-down.
export function BreakdownChart({
  title,
  description,
  data,
  kind,
  label,
}: {
  title: string
  description?: string
  data: Slice[]
  kind: ValueKind
  label: string
}) {
  const config = { value: { label, color: "var(--chart-2)" } } satisfies ChartConfig
  const sorted = [...data].sort((a, b) => b.value - a.value)
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>
        <ChartContainer config={config} className="aspect-auto h-[260px] w-full">
          <BarChart data={sorted} layout="vertical" margin={{ left: 8, right: 16 }}>
            <CartesianGrid horizontal={false} />
            <XAxis type="number" dataKey="value" tickLine={false} axisLine={false}
              tickFormatter={(v: number) => formatCompact(v, kind)} />
            <YAxis dataKey="category" type="category" tickLine={false} axisLine={false} width={128} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Bar dataKey="value" fill="var(--color-value)" radius={4} />
          </BarChart>
        </ChartContainer>
      </CardContent>
    </Card>
  )
}
'''

MASKED_VALUE_TSX = '''\
"use client"

import * as React from "react"
import { Eye, EyeOff } from "lucide-react"

import { Button } from "@/components/ui/button"

// Restricted values (personal or health data) stay hidden until asked for.
export function MaskedValue({ value }: { value: string }) {
  const [shown, setShown] = React.useState(false)
  return (
    <span className="inline-flex items-center gap-1">
      <span className="font-mono">{shown ? value : "••••••"}</span>
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={shown ? "Hide value" : "Show value"}
        onClick={() => setShown((s) => !s)}
      >
        {shown ? <EyeOff /> : <Eye />}
      </Button>
    </span>
  )
}
'''

RECORD_SHEET_TSX = '''\
"use client"

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { MaskedValue } from "@/components/kb/masked-value"
import { formatValue, type ValueKind } from "@/lib/kb-format"

export type FieldSpec = {
  key: string
  label: string
  kind: ValueKind
  restricted?: boolean
}

// The whole record in a side sheet, opened from a table row.
export function RecordSheet({
  record,
  fields,
  title,
  description,
  onOpenChange,
}: {
  record: Record<string, unknown> | null
  fields: FieldSpec[]
  title: string
  description?: string
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Sheet open={record !== null} onOpenChange={onOpenChange}>
      <SheetContent className="overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{title}</SheetTitle>
          {description ? <SheetDescription>{description}</SheetDescription> : null}
        </SheetHeader>
        {record ? (
          <dl className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-x-4 gap-y-2 px-4 pb-6 text-sm">
            {fields.map((f) => (
              <div key={f.key} className="contents">
                <dt className="text-muted-foreground">{f.label}</dt>
                <dd className="break-words">
                  {f.restricted ? (
                    <MaskedValue value={String(record[f.key] ?? "")} />
                  ) : (
                    formatValue(record[f.key], f.kind)
                  )}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
      </SheetContent>
    </Sheet>
  )
}
'''

ENUM_SELECT = {
    "base": '''\
"use client"

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

// Base UI Select: pass the items so the trigger can show the chosen label.
export function EnumSelect({
  value,
  onChange,
  options,
  labels = {},
  placeholder,
  id,
  className,
  invalid,
}: {
  value: string | undefined
  onChange: (value: string) => void
  options: string[]
  labels?: Record<string, string>
  placeholder?: string
  id?: string
  className?: string
  invalid?: boolean
}) {
  const items = options.map((o) => ({ value: o, label: labels[o] ?? o }))
  return (
    <Select items={items} value={value ?? null} onValueChange={(v) => onChange((v as string | null) ?? "")}>
      <SelectTrigger id={id} className={className ?? "w-full"} aria-invalid={invalid}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
''',
    "radix": '''\
"use client"

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export function EnumSelect({
  value,
  onChange,
  options,
  labels = {},
  placeholder,
  id,
  className,
  invalid,
}: {
  value: string | undefined
  onChange: (value: string) => void
  options: string[]
  labels?: Record<string, string>
  placeholder?: string
  id?: string
  className?: string
  invalid?: boolean
}) {
  return (
    <Select value={value} onValueChange={(v) => onChange(v)}>
      <SelectTrigger id={id} className={className ?? "w-full"} aria-invalid={invalid}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o} value={o}>
            {labels[o] ?? o}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
''',
}

_DATE_FIELD_HEAD = '''\
"use client"

import { format } from "date-fns"
import { CalendarIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Calendar } from "@/components/ui/calendar"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"

// The shadcn Date Picker pattern: a Popover holding a Calendar.
export function DateField({
  value,
  onChange,
  id,
  invalid,
}: {
  value: Date | undefined
  onChange: (value: Date | undefined) => void
  id?: string
  invalid?: boolean
}) {
  const button = (
    <Button
      id={id}
      variant="outline"
      aria-invalid={invalid}
      data-empty={!value}
      className="w-full justify-start text-left font-normal data-[empty=true]:text-muted-foreground"
    />
  )
  const label = (
    <>
      <CalendarIcon />
      {value ? format(value, "PPP") : <span>Pick a date</span>}
    </>
  )
  return (
    <Popover>
'''
_DATE_FIELD_TAIL = '''\
      <PopoverContent className="w-auto p-0" align="start">
        <Calendar mode="single" selected={value} onSelect={onChange} defaultMonth={value} />
      </PopoverContent>
    </Popover>
  )
}
'''
DATE_FIELD = {
    "base": _DATE_FIELD_HEAD + "      <PopoverTrigger render={button}>{label}</PopoverTrigger>\n"
    + _DATE_FIELD_TAIL,
    "radix": _DATE_FIELD_HEAD.replace("  const button = (\n    <Button", "  const button = (props: { children: React.ReactNode }) => (\n    <Button")
    .replace('      className="w-full justify-start text-left font-normal data-[empty=true]:text-muted-foreground"\n    />',
             '      className="w-full justify-start text-left font-normal data-[empty=true]:text-muted-foreground"\n    >\n      {props.children}\n    </Button>')
    .replace('"use client"\n\n', '"use client"\n\nimport * as React from "react"\n', 1)
    + "      <PopoverTrigger asChild>{button({ children: label })}</PopoverTrigger>\n"
    + _DATE_FIELD_TAIL,
}

_SHELL_HEAD = '''\
"use client"

import * as React from "react"
import { FilePlus2, LayoutDashboard, Table2 } from "lucide-react"

import { Separator } from "@/components/ui/separator"
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { appTitle, nav } from "@/nav"

const ICONS = { dashboard: LayoutDashboard, list: Table2, form: FilePlus2 } as const

// The app frame: a collapsible shadcn Sidebar listing the pages, and the page
// title in a header with the sidebar toggle.
export function AppShell({
  active,
  title,
  children,
}: {
  active: string
  title: string
  children: React.ReactNode
}) {
  return (
    <SidebarProvider>
      <Sidebar>
        <SidebarHeader>
          <div className="px-2 py-1 text-sm font-semibold">{appTitle}</div>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Pages</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {nav.map((item) => {
                  const Icon = ICONS[item.archetype]
                  return (
                    <SidebarMenuItem key={item.slug}>
'''
_SHELL_TAIL = '''\
                    </SidebarMenuItem>
                  )
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
      </Sidebar>
      <SidebarInset>
        <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-2 h-4" />
          <h1 className="text-base font-medium">{title}</h1>
        </header>
        <main className="flex flex-1 flex-col gap-4 p-4 lg:p-6">{children}</main>
      </SidebarInset>
    </SidebarProvider>
  )
}
'''
APP_SHELL = {
    "base": _SHELL_HEAD + '''\
                      <SidebarMenuButton isActive={item.slug === active} render={<a href={item.href} />}>
                        <Icon />
                        <span>{item.title}</span>
                      </SidebarMenuButton>
''' + _SHELL_TAIL,
    "radix": _SHELL_HEAD + '''\
                      <SidebarMenuButton isActive={item.slug === active} asChild>
                        <a href={item.href}>
                          <Icon />
                          <span>{item.title}</span>
                        </a>
                      </SidebarMenuButton>
''' + _SHELL_TAIL,
}


# ---------------------------------------------------------------------------
# Sample data (deterministic; never real people)
# ---------------------------------------------------------------------------

def _rng(*parts: str) -> random.Random:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


_BASE_DAY = date(2026, 1, 1)


def sample_value(f: us.Field, i: int, rnd: random.Random) -> Any:
    k = f.kind
    if f.restricted:
        t = ts_type(f)
        if t == "number":
            return 900000 + i
        if t == "boolean":
            return False
        if k in ("date", "timestamp"):
            return "1900-01-01"
        if k == "enum" and f.enum_values:
            return f.enum_values[0]
        return f"SAMPLE-{f.name[:3].upper()}-{1000 + i}"
    if k == "id":
        return f"{f.name[:3].upper()}-{10001 + i}" if ts_type(f) == "string" else 10001 + i
    if k == "fk":
        return f"REF-{rnd.randint(100, 999)}" if ts_type(f) == "string" else rnd.randint(1, 500)
    if k == "enum":
        vals = f.enum_values or [f"{f.label} A", f"{f.label} B", f"{f.label} C"]
        return vals[(i * 7 + rnd.randint(0, 2)) % len(vals)]
    if k == "boolean":
        return rnd.random() < 0.3
    if k in ("date", "timestamp"):
        d = _BASE_DAY + timedelta(days=(i * 11 + rnd.randint(0, 9)) % 330)
        return d.isoformat()
    if k == "currency":
        return round(rnd.uniform(80, 12000), 2)
    if k == "percent":
        return round(rnd.uniform(2, 40), 1)
    if k == "integer":
        return rnd.randint(1, 40)
    if k == "number":
        return round(rnd.uniform(1, 100), 2)
    if k == "longtext":
        return f"Sample {f.label.lower()} text for row {i + 1}."
    if re.search(r"(^|_)(number|code|reference|ref)$", f.name):
        return f"{f.name[:2].upper()}{rnd.randint(100000, 999999)}"
    return f"Sample {f.label.lower()} {i + 1}"


def sample_rows(e: us.Entity, n: int = 24) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        rnd = _rng(e.name, str(i))
        out.append({f.name: sample_value(f, i, rnd) for f in e.fields})
    return out


def sample_series(m: us.Metric, months: int = 12) -> list[dict[str, Any]]:
    rnd = _rng("series", m.name)
    base = {"currency": 250000.0, "percent": 12.0, "integer": 1200.0}.get(m.format, 100.0)
    out, v = [], base
    for i in range(months):
        v = max(0.0, v * (1 + rnd.uniform(-0.08, 0.1)))
        mon = _BASE_DAY.replace(month=1 + i % 12, year=_BASE_DAY.year + i // 12)
        out.append({"period": mon.strftime("%b %Y"),
                    "value": round(v, 1 if m.format == "percent" else 0)})
    return out


def sample_breakdown(m: us.Metric, dim: Optional[us.Dimension]) -> list[dict[str, Any]]:
    rnd = _rng("breakdown", m.name, dim.name if dim else "")
    cats = (dim.values if dim and dim.values else
            [f"{dim.label if dim else 'Category'} {c}" for c in "ABCDE"])
    base = {"currency": 80000.0, "percent": 15.0, "integer": 400.0}.get(m.format, 50.0)
    return [{"category": c, "value": round(base * rnd.uniform(0.2, 1.4),
                                           1 if m.format == "percent" else 0)} for c in cats[:8]]


# ---------------------------------------------------------------------------
# Generated per-spec files
# ---------------------------------------------------------------------------

def _href(stack: str, slug: str) -> str:
    return f"#/{slug}" if stack == "vite" else f"/{slug}"


def nav_ts(spec: us.AppSpec) -> str:
    items = [{"slug": p.slug, "title": p.title, "archetype": p.archetype,
              "href": _href(spec.stack, p.slug)} for p in spec.pages]
    return (f"// Pages of {spec.title}. Generated by myPub /kb-ui from ui-spec.json.\n"
            f"export const appTitle = {_js(spec.title)}\n\n"
            "export type NavItem = {\n  slug: string\n  title: string\n"
            '  archetype: "dashboard" | "list" | "form"\n  href: string\n}\n\n'
            f"export const nav: NavItem[] = {json.dumps(items, indent=2, ensure_ascii=False)}\n")


def types_ts(spec: us.AppSpec) -> str:
    lines = ["// Row types, one per entity. Generated by myPub /kb-ui from the contract/DDL.", ""]
    for e in spec.entities:
        lines.append(f"export type {us.pascal(e.name)} = {{")
        for f in e.fields:
            opt = "" if f.required else "?"
            null = "" if f.required else " | null"
            comment = f"  /** {_doc(f.description)} */\n" if f.description else ""
            lines.append(f"{comment}  {_prop(f.name)}{opt}: {ts_type(f)}{null}")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def _doc(text: str) -> str:
    return re.sub(r"\*/", "* /", " ".join(text.split()))[:200]


def _prop(name: str) -> str:
    return name if re.match(r"^[A-Za-z_$][\w$]*$", name) else _js(name)


def sample_ts(spec: us.AppSpec, rows: dict[str, list[dict]], series: dict[str, list[dict]],
              breakdowns: dict[str, list[dict]]) -> str:
    types = ", ".join(us.pascal(e.name) for e in spec.entities)
    lines = ["// Sample data, generated deterministically by myPub /kb-ui.",
             "// Not real records: restricted columns hold SAMPLE- placeholders.",
             'import type { Point } from "@/components/kb/trend-chart"',
             'import type { Slice } from "@/components/kb/breakdown-chart"']
    if types:
        lines.append(f'import type {{ {types} }} from "@/data/types"')
    lines.append("")
    for e in spec.entities:
        lines.append(f"export const sample{us.pascal(e.plural if e.plural else e.name)}: "
                     f"{us.pascal(e.name)}[] = {_js_rows(rows[e.name])}")
        lines.append("")
    for name, pts in series.items():
        lines.append(f"export const series_{name}: Point[] = {_js_rows(pts)}")
    for name, sl in breakdowns.items():
        lines.append(f"export const breakdown_{name}: Slice[] = {_js_rows(sl)}")
    return "\n".join(lines) + "\n"


def _js_rows(rows: list[dict]) -> str:
    if not rows:
        return "[]"
    return "[\n" + "".join("  " + json.dumps(r, ensure_ascii=False) + ",\n" for r in rows) + "]"


def source_ts(spec: us.AppSpec, series: dict[str, list[dict]],
              breakdowns: dict[str, list[dict]]) -> str:
    """The data-access seam: async functions the views call, returning sample data
    now. Replace the bodies to read a real source (one function per entity/metric)."""
    imports = [f"sample{us.pascal(e.plural)}" for e in spec.entities] + \
        [f"series_{n}" for n in series] + [f"breakdown_{n}" for n in breakdowns]
    types = ", ".join(us.pascal(e.name) for e in spec.entities)
    lines = [
        "// Data access for the views. Every function returns sample data today.",
        "// To go live, replace a body with a call to your API: for a Databricks",
        "// table, a server route that runs the query through the SQL Statement",
        "// Execution API (Next.js: a Route Handler; Vite: your backend). Keep the",
        "// return types; the views depend on them.",
    ]
    if imports:
        lines.append("import {\n" + "".join(f"  {i},\n" for i in imports) + '} from "@/data/sample"')
    lines += ['import type { Point } from "@/components/kb/trend-chart"',
              'import type { Slice } from "@/components/kb/breakdown-chart"']
    if types:
        lines.append(f'import type {{ {types} }} from "@/data/types"')
    lines.append("")
    for e in spec.entities:
        lines.append(f"export async function list{us.pascal(e.plural)}(): "
                     f"Promise<{us.pascal(e.name)}[]> {{")
        lines.append(f"  return sample{us.pascal(e.plural)}")
        lines.append("}")
        lines.append("")
    for n in series:
        lines += [f"export async function trend_{n}(): Promise<Point[]> {{",
                  f"  return series_{n}", "}", ""]
    for n in breakdowns:
        lines += [f"export async function breakdown{us.pascal(n)}(): Promise<Slice[]> {{",
                  f"  return breakdown_{n}", "}", ""]
    return "\n".join(lines)


def _columns_js(fields: Sequence[us.Field]) -> str:
    return "[\n" + "".join(
        f"  {{ key: {_js(f.name)}, label: {_js(f.label)}, kind: {_js(col_kind(f))} }},\n"
        for f in fields) + "]"


def _record_fields_js(e: us.Entity) -> str:
    fields = [f for f in e.fields if not us.is_audit(f.name)]
    return "[\n" + "".join(
        f"  {{ key: {_js(f.name)}, label: {_js(f.label)}, kind: {_js(col_kind(f))}"
        + (", restricted: true" if f.restricted else "") + " },\n" for f in fields) + "]"


def list_view(spec: us.AppSpec, page: us.Page) -> str:
    e = spec.entity(page.entity)
    assert e is not None
    region = next(r for r in page.regions if r.role == "table")
    shown = [f for f in e.fields if f.name in region.bindings.get("columns", [])]
    shown.sort(key=lambda f: region.bindings["columns"].index(f.name))
    search = region.bindings.get("search")
    facets = [f for f in e.fields if f.name in region.bindings.get("facets", [])]
    T = us.pascal(e.name)
    P = us.pascal(e.plural)
    form = not spec.read_only and any(r.role == "form" for r in page.regions)
    facet_js = "[\n" + "".join(
        f"  {{ key: {_js(f.name)}, label: {_js(f.label)}, options: {_js(f.enum_values or [])} }},\n"
        for f in facets) + "]"
    pk = e.pk.name if e.pk else (shown[0].name if shown else "")
    form_imports = (f'import {{ {T}Form }} from "@/views/{us.slug(e.name)}-form"\n'
                    'import { Button } from "@/components/ui/button"\n'
                    'import {\n  Sheet,\n  SheetContent,\n  SheetDescription,\n  SheetHeader,\n'
                    '  SheetTitle,\n} from "@/components/ui/sheet"\n') if form else ""
    form_state = "  const [creating, setCreating] = React.useState(false)\n" if form else ""
    form_button = (f'        <Button onClick={{() => setCreating(true)}}>New {e.label.lower()}</Button>\n'
                   if form else "")
    form_sheet = (f'''      <Sheet open={{creating}} onOpenChange={{setCreating}}>
        <SheetContent className="overflow-y-auto sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>New {e.label.lower()}</SheetTitle>
            <SheetDescription>Fields marked with * are required.</SheetDescription>
          </SheetHeader>
          <div className="px-4 pb-6">
            <{T}Form onSaved={{() => setCreating(false)}} />
          </div>
        </SheetContent>
      </Sheet>
''' if form else "")
    desc = page.description or e.description
    return f'''"use client"

import * as React from "react"

import {{ DataTable, type ColumnSpec, type Facet, type Row }} from "@/components/kb/data-table"
import {{ RecordSheet, type FieldSpec }} from "@/components/kb/record-sheet"
{form_imports}import {{ list{P} }} from "@/data/source"

const columns: ColumnSpec[] = {_columns_js(shown)}

const facets: Facet[] = {facet_js}

const recordFields: FieldSpec[] = {_record_fields_js(e)}

// {page.title}: a Data Table of {e.plural.lower()} with search, facets, sorting and
// paging; a row opens the full record in a side sheet.
export function {us.pascal(page.slug)}View() {{
  const [rows, setRows] = React.useState<Row[]>([])
  const [selected, setSelected] = React.useState<Row | null>(null)
{form_state}
  React.useEffect(() => {{
    list{P}().then((data) => setRows(data as unknown as Row[]))
  }}, [])

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-4">
        <p className="max-w-3xl text-sm text-muted-foreground">
          {_jsx_text(_first_sentence(desc))}
        </p>
{form_button}      </div>
      <DataTable
        data={{rows}}
        columns={{columns}}
        searchKey={_js(search) if search else "undefined"}
        searchLabel={_js(_field_label(e, search).lower()) if search else "undefined"}
        facets={{facets}}
        onRowClick={{setSelected}}
      />
      <RecordSheet
        record={{selected}}
        fields={{recordFields}}
        title={{selected ? `{e.label} ${{String(selected[{_js(pk)}] ?? "")}}` : ""}}
        onOpenChange={{(open) => !open && setSelected(null)}}
      />
{form_sheet}    </div>
  )
}}
'''.replace('searchKey="undefined"', "searchKey={undefined}").replace(
        'searchLabel="undefined"', "searchLabel={undefined}")


def _first_sentence(text: str, limit: int = 240) -> str:
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return (m.group(1) if m else t)[:limit]


def _field_label(e: us.Entity, name: Optional[str]) -> str:
    f = next((f for f in e.fields if f.name == name), None)
    return f.label if f else (name or "")


def _jsx_text(text: str) -> str:
    t = " ".join((text or "").split())[:280]
    return "{" + _js(t) + "}" if t else ""


def dashboard_view(spec: us.AppSpec, page: us.Page) -> str:
    kpis = [r for r in page.regions if r.role == "kpi"]
    trend = next((r for r in page.regions if r.role == "trend"), None)
    brk = next((r for r in page.regions if r.role == "breakdown"), None)
    table = next((r for r in page.regions if r.role == "table"), None)
    imports = ['import * as React from "react"', "",
               'import { KpiCard } from "@/components/kb/kpi-card"']
    src_fns: list[str] = []
    body: list[str] = []
    state: list[str] = []
    effects: list[str] = []
    if kpis:
        cards = []
        for r in kpis:
            for b in r.bindings.get("cards", []):
                name = b["metric"]
                src_fns.append(f"trend_{name}")
                state.append(f"  const [{camel_name(name)}, set{us.pascal(name)}] = "
                             "React.useState<Point[]>([])")
                effects.append(f"    trend_{name}().then(set{us.pascal(name)})")
                v = camel_name(name)
                cards.append(f'''        <KpiCard
          label={_js(b["label"])}
          value={{{v}.at(-1)?.value ?? 0}}
          previous={{{v}.at(-2)?.value}}
          kind={_js(b["kind"])}
          note={_js(b.get("note") or "Latest month against the month before")}
        />''')
        body.append('      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">\n'
                    + "\n".join(cards) + "\n      </div>")
    charts = []
    if trend:
        b = trend.bindings
        imports.append('import { TrendChart, type Point } from "@/components/kb/trend-chart"')
        if f"trend_{b['metric']}" not in src_fns:
            src_fns.append(f"trend_{b['metric']}")
            state.append(f"  const [{camel_name(b['metric'])}, set{us.pascal(b['metric'])}] = "
                         "React.useState<Point[]>([])")
            effects.append(f"    trend_{b['metric']}().then(set{us.pascal(b['metric'])})")
        charts.append(f'''        <TrendChart
          title={_js(trend.title)}
          description={_js(b.get("description", ""))}
          data={{{camel_name(b["metric"])}}}
          kind={_js(b["kind"])}
          label={_js(b["label"])}
        />''')
    elif kpis:
        imports.append('import type { Point } from "@/components/kb/trend-chart"')
    if brk:
        b = brk.bindings
        imports.append('import { BreakdownChart, type Slice } from "@/components/kb/breakdown-chart"')
        fn = f"breakdown{us.pascal(b['key'])}"
        src_fns.append(fn)
        state.append(f"  const [slices, setSlices] = React.useState<Slice[]>([])")
        effects.append(f"    {fn}().then(setSlices)")
        charts.append(f'''        <BreakdownChart
          title={_js(brk.title)}
          description={_js(b.get("description", ""))}
          data={{slices}}
          kind={_js(b["kind"])}
          label={_js(b["label"])}
        />''')
    if charts:
        body.append('      <div className="grid gap-4 lg:grid-cols-2">\n' + "\n".join(charts)
                    + "\n      </div>")
    if table:
        e = spec.entity(table.bindings["entity"])
        assert e is not None
        P = us.pascal(e.plural)
        shown = [f for f in e.fields if f.name in table.bindings.get("columns", [])]
        shown.sort(key=lambda f: table.bindings["columns"].index(f.name))
        imports.append('import { DataTable, type ColumnSpec, type Row } from "@/components/kb/data-table"')
        src_fns.append(f"list{P}")
        state.append("  const [rows, setRows] = React.useState<Row[]>([])")
        effects.append(f"    list{P}().then((data) => setRows(data as unknown as Row[]))")
        body.append(f'''      <section className="flex flex-col gap-1">
        <h2 className="text-base font-medium">{_jsx_text(table.title)}</h2>
        <DataTable data={{rows}} columns={{tableColumns}} />
      </section>''')
        pre = f"\nconst tableColumns: ColumnSpec[] = {_columns_js(shown)}\n"
    else:
        pre = ""
    imports.append("import {\n" + "".join(f"  {f},\n" for f in src_fns) + '} from "@/data/source"')
    return ('"use client"\n\n' + "\n".join(imports) + "\n" + pre + f'''
// {page.title}: headline KPIs, the trend of the main measure and its breakdown,
// from the product's metric views.
export function {us.pascal(page.slug)}View() {{
{chr(10).join(state)}

  React.useEffect(() => {{
{chr(10).join(effects)}
  }}, [])

  return (
    <div className="flex flex-col gap-4">
{chr(10).join(body)}
    </div>
  )
}}
''')


def camel_name(name: str) -> str:
    return us.camel(name) + "Series"


def form_view(spec: us.AppSpec, e: us.Entity, fields: Sequence[us.Field]) -> str:
    T = us.pascal(e.name)
    need_date = any(f.kind in ("date", "timestamp") for f in fields)
    need_select = any(f.kind == "enum" and f.enum_values and len(f.enum_values) > 4 for f in fields)
    need_radio = any(f.kind == "enum" and f.enum_values and len(f.enum_values) <= 4 for f in fields)
    need_switch = any(f.kind == "boolean" for f in fields)
    need_textarea = any(f.kind == "longtext" for f in fields)
    imports = ['import * as React from "react"',
               'import { zodResolver } from "@hookform/resolvers/zod"',
               'import { Controller, useForm } from "react-hook-form"',
               'import * as z from "zod"', "",
               'import { Button } from "@/components/ui/button"',
               'import {\n  Field,\n  FieldDescription,\n  FieldError,\n  FieldGroup,\n  FieldLabel,\n'
               '} from "@/components/ui/field"',
               'import { Input } from "@/components/ui/input"']
    if need_textarea:
        imports.append('import { Textarea } from "@/components/ui/textarea"')
    if need_switch:
        imports.append('import { Switch } from "@/components/ui/switch"')
    if need_radio:
        imports.append('import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"')
    if need_select:
        imports.append('import { EnumSelect } from "@/components/kb/enum-select"')
    if need_date:
        imports.append('import { DateField } from "@/components/kb/date-field"')
    imports.append(f'import {{ {us.camel(e.name)}Schema, type {T}FormValues }} from "@/lib/schemas"')
    defaults = []
    for f in fields:
        if f.kind == "boolean":
            defaults.append(f"    {_prop(f.name)}: false,")
        elif f.kind in ("text", "longtext") or (f.kind == "enum" and not f.enum_values) or \
                (f.kind == "fk" and ts_type(f) == "string"):
            defaults.append(f'    {_prop(f.name)}: "",')
        else:
            defaults.append(f"    {_prop(f.name)}: undefined,")
    controls = "\n".join(_control(f, e) for f in fields)
    return f'''"use client"

{chr(10).join(imports)}

// New {e.label.lower()}: React Hook Form + zod, rendered with shadcn Field
// components (the pattern in the shadcn Forms docs). Validation rules come
// from the column types and required flags; see lib/schemas.ts.
export function {T}Form({{ onSaved }}: {{ onSaved?: (values: {T}FormValues) => void }}) {{
  const [saved, setSaved] = React.useState<{T}FormValues | null>(null)
  const form = useForm<{T}FormValues>({{
    resolver: zodResolver({us.camel(e.name)}Schema),
    defaultValues: {{
{chr(10).join(defaults)}
    }},
  }})

  function onSubmit(values: {T}FormValues) {{
    // TODO: send to your API (see data/source.ts). Until then, show what would be saved.
    setSaved(values)
    onSaved?.(values)
  }}

  return (
    <form id="{us.slug(e.name)}-form" onSubmit={{form.handleSubmit(onSubmit)}} className="flex flex-col gap-6">
      <FieldGroup>
{controls}
      </FieldGroup>
      <Field orientation="horizontal">
        <Button type="button" variant="outline" onClick={{() => form.reset()}}>
          Reset
        </Button>
        <Button type="submit">Save</Button>
      </Field>
      {{saved ? (
        <pre className="overflow-x-auto rounded-md bg-muted p-4 text-xs">
          {{JSON.stringify(saved, null, 2)}}
        </pre>
      ) : null}}
    </form>
  )
}}
'''


def _control(f: us.Field, e: us.Entity) -> str:
    fid = f"{us.slug(e.name)}-{us.slug(f.name)}"
    lab = f.label + (" *" if f.required else "")
    desc = ""
    if f.kind == "fk":
        desc = f'\n              <FieldDescription>Id of the related {us.label(f.references or f.name.removesuffix("_id")).lower()}. TODO: replace with a lookup.</FieldDescription>'
    elif f.kind == "enum" and not f.enum_values:
        desc = "\n              <FieldDescription>Allowed values aren't in the contract yet. TODO: add them.</FieldDescription>"
    elif f.restricted:
        desc = "\n              <FieldDescription>Restricted: personal or health data.</FieldDescription>"
    k = f.kind
    if k == "boolean":
        return f'''        <Controller
          name={_js(f.name)}
          control={{form.control}}
          render={{({{ field }}) => (
            <Field orientation="horizontal">
              <Switch id="{fid}" checked={{!!field.value}} onCheckedChange={{(c) => field.onChange(c)}} />
              <FieldLabel htmlFor="{fid}">{_esc(lab)}</FieldLabel>
            </Field>
          )}}
        />'''
    if k in ("date", "timestamp"):
        inner = f'<DateField id="{fid}" value={{field.value as Date | undefined}} onChange={{field.onChange}} invalid={{fieldState.invalid}} />'
    elif k == "enum" and f.enum_values and len(f.enum_values) <= 4:
        items = "\n".join(
            f'''                <Field orientation="horizontal">
                  <RadioGroupItem value={_js(v)} id="{fid}-{us.slug(v) or i}" />
                  <FieldLabel htmlFor="{fid}-{us.slug(v) or i}" className="font-normal">{_esc(v)}</FieldLabel>
                </Field>''' for i, v in enumerate(f.enum_values))
        inner = (f'<RadioGroup value={{(field.value as string | undefined) ?? ""}} '
                 f'onValueChange={{(v) => field.onChange(v as string)}} aria-invalid={{fieldState.invalid}}>\n'
                 f'{items}\n              </RadioGroup>')
    elif k == "enum" and f.enum_values:
        inner = (f'<EnumSelect id="{fid}" value={{field.value as string | undefined}} '
                 f'onChange={{field.onChange}} options={{{_js(f.enum_values)}}} '
                 f'placeholder="Choose {_esc(f.label.lower())}" invalid={{fieldState.invalid}} />')
    elif k == "longtext":
        inner = (f'<Textarea {{...field}} id="{fid}" value={{(field.value as string | undefined) ?? ""}} '
                 f'rows={{4}} aria-invalid={{fieldState.invalid}} />')
    elif k in ("integer", "number", "currency", "percent") or (k == "fk" and ts_type(f) == "number"):
        step = ' step="0.01"' if k == "currency" else (' step="any"' if k in ("number", "percent") else "")
        inner = (f'<Input id="{fid}" type="number"{step} name={{field.name}} onBlur={{field.onBlur}} '
                 f'ref={{field.ref}} value={{field.value === undefined || field.value === null ? "" : String(field.value)}} '
                 f'onChange={{(e) => field.onChange(e.target.value === "" ? undefined : Number(e.target.value))}} '
                 f'aria-invalid={{fieldState.invalid}} />')
    else:
        inner = (f'<Input {{...field}} id="{fid}" value={{(field.value as string | undefined) ?? ""}} '
                 f'autoComplete="off" aria-invalid={{fieldState.invalid}} />')
    return f'''        <Controller
          name={_js(f.name)}
          control={{form.control}}
          render={{({{ field, fieldState }}) => (
            <Field data-invalid={{fieldState.invalid}}>
              <FieldLabel htmlFor="{fid}">{_esc(lab)}</FieldLabel>
              {inner}{desc}
              {{fieldState.invalid && <FieldError errors={{[fieldState.error]}} />}}
            </Field>
          )}}
        />'''


def _esc(text: str) -> str:
    return re.sub(r"[{}<>]", "", text or "")


def schemas_ts(spec: us.AppSpec, forms: dict[str, list[us.Field]]) -> str:
    lines = ['import * as z from "zod"', "",
             "// Form schemas, one per entity with a form. Rules come from the column",
             "// types, required flags and allowed values in the contract or DDL.", ""]
    for name, fields in forms.items():
        e = spec.entity(name)
        assert e is not None
        lines.append(f"export const {us.camel(e.name)}Schema = z.object({{")
        for f in fields:
            lines.append(f"  {_prop(f.name)}: {us.zod_for(f)},")
        lines.append("})")
        lines.append(f"export type {us.pascal(e.name)}FormValues = z.infer<typeof {us.camel(e.name)}Schema>")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stack shells
# ---------------------------------------------------------------------------

def vite_app(spec: us.AppSpec) -> str:
    imports = "".join(f'import {{ {us.pascal(p.slug)}View }} from "@/views/{p.slug}"\n'
                      for p in spec.pages)
    routes = "".join(f'  {_js(p.slug)}: {{ title: {_js(p.title)}, View: {us.pascal(p.slug)}View }},\n'
                     for p in spec.pages)
    first = spec.pages[0].slug
    return f'''import * as React from "react"

import {{ AppShell }} from "@/components/kb/app-shell"
{imports}
// Hash routes (#/<page>), so the app needs no router package and works from
// any static host. Generated by myPub /kb-ui.
const routes: Record<string, {{ title: string; View: () => React.JSX.Element }}> = {{
{routes}}}

function currentSlug(): string {{
  const slug = window.location.hash.replace(/^#\\/?/, "")
  return slug in routes ? slug : {_js(first)}
}}

export default function App() {{
  const [slug, setSlug] = React.useState(currentSlug)

  React.useEffect(() => {{
    const onHash = () => setSlug(currentSlug())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }}, [])

  const {{ title, View }} = routes[slug]
  return (
    <AppShell active={{slug}} title={{title}}>
      <View />
    </AppShell>
  )
}}
'''


def next_page(p: us.Page) -> str:
    return f'''import {{ AppShell }} from "@/components/kb/app-shell"
import {{ {us.pascal(p.slug)}View }} from "@/views/{p.slug}"

export const metadata = {{ title: {_js(p.title)} }}

export default function Page() {{
  return (
    <AppShell active={_js(p.slug)} title={_js(p.title)}>
      <{us.pascal(p.slug)}View />
    </AppShell>
  )
}}
'''


def next_home(spec: us.AppSpec) -> str:
    return f'''import {{ redirect }} from "next/navigation"

export default function Home() {{
  redirect({_js("/" + spec.pages[0].slug)})
}}
'''


# ---------------------------------------------------------------------------
# All files
# ---------------------------------------------------------------------------

def emit(spec: us.AppSpec) -> dict[str, str]:
    """Overlay files keyed by path under the project's @/ root (src/)."""
    files: dict[str, str] = {
        "lib/kb-format.ts": FORMAT_TS,
        "components/kb/data-table.tsx": DATA_TABLE_TSX,
        "components/kb/kpi-card.tsx": KPI_CARD_TSX,
        "components/kb/trend-chart.tsx": TREND_CHART_TSX,
        "components/kb/breakdown-chart.tsx": BREAKDOWN_CHART_TSX,
        "components/kb/masked-value.tsx": MASKED_VALUE_TSX,
        "components/kb/record-sheet.tsx": RECORD_SHEET_TSX,
        "components/kb/enum-select.tsx": ENUM_SELECT[spec.base],
        "components/kb/app-shell.tsx": APP_SHELL[spec.base],
        "nav.ts": nav_ts(spec),
        "data/types.ts": types_ts(spec),
    }
    rows = {e.name: sample_rows(e) for e in spec.entities}
    series: dict[str, list[dict]] = {}
    breakdowns: dict[str, list[dict]] = {}
    forms: dict[str, list[us.Field]] = {}
    for p in spec.pages:
        for r in p.regions:
            if r.role == "kpi":
                for b in r.bindings.get("cards", []):
                    m = spec.metric(b["metric"])
                    if m:
                        series.setdefault(m.name, sample_series(m))
            elif r.role == "trend":
                m = spec.metric(r.bindings["metric"])
                if m:
                    series.setdefault(m.name, sample_series(m))
            elif r.role == "breakdown":
                m = spec.metric(r.bindings["metric"])
                dim = next((d for d in (m.category_dims if m else [])
                            if d.name == r.bindings.get("dimension")), None)
                if m:
                    breakdowns.setdefault(r.bindings["key"], sample_breakdown(m, dim))
            elif r.role == "form" and p.entity:
                e = spec.entity(p.entity)
                if e:
                    names = r.bindings.get("fields", [])
                    forms[e.name] = sorted([f for f in e.fields if f.name in names],
                                           key=lambda f: names.index(f.name))
    files["data/sample.ts"] = sample_ts(spec, rows, series, breakdowns)
    files["data/source.ts"] = source_ts(spec, series, breakdowns)
    if any(f.kind in ("date", "timestamp") for fs in forms.values() for f in fs):
        files["components/kb/date-field.tsx"] = DATE_FIELD[spec.base]
    if forms:
        files["lib/schemas.ts"] = schemas_ts(spec, forms)
        for name, fields in forms.items():
            e = spec.entity(name)
            assert e is not None
            files[f"views/{us.slug(e.name)}-form.tsx"] = form_view(spec, e, fields)
    for p in spec.pages:
        if p.archetype == "dashboard":
            files[f"views/{p.slug}.tsx"] = dashboard_view(spec, p)
        elif p.archetype == "list":
            files[f"views/{p.slug}.tsx"] = list_view(spec, p)
    if spec.stack == "vite":
        files["App.tsx"] = vite_app(spec)
    else:
        files["app/page.tsx"] = next_home(spec)
        for p in spec.pages:
            files[f"app/{p.slug}/page.tsx"] = next_page(p)
    return files
