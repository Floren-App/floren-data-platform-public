// Generate Markdown directly from the provider's schemas. No Configure/API calls.
package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/floren/terraform-provider-fdp/internal/provider"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	ps "github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/resource"
)

type attribute interface {
	GetDescription() string
	GetType() attr.Type
	IsRequired() bool
	IsOptional() bool
	IsComputed() bool
	IsSensitive() bool
}

func cell(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, "|", "\\|"), "\n", " ")
}

func table(attributes map[string]attribute) string {
	names := make([]string, 0, len(attributes))
	for name := range attributes {
		names = append(names, name)
	}
	sort.Strings(names)
	s := "\n| Attribute | Type | Behavior | Description |\n|---|---|---|---|\n"
	for _, name := range names {
		a := attributes[name]
		var flags []string
		if a.IsRequired() {
			flags = append(flags, "Required")
		}
		if a.IsOptional() {
			flags = append(flags, "Optional")
		}
		if a.IsComputed() {
			flags = append(flags, "Computed")
		}
		if a.IsSensitive() {
			flags = append(flags, "Sensitive")
		}
		if v, ok := a.(interface{ IsWriteOnly() bool }); ok && v.IsWriteOnly() {
			flags = append(flags, "Write-only")
		}
		s += fmt.Sprintf("| `%s` | `%s` | %s | %s |\n", name, cell(a.GetType().String()), strings.Join(flags, ", "), cell(a.GetDescription()))
	}
	return s
}

func main() {
	if len(os.Args) != 2 {
		panic("usage: go run ./cmd/docs <output-directory>")
	}
	out := os.Args[1]
	if err := os.MkdirAll(out, 0755); err != nil {
		panic(err)
	}
	write := func(name, text string) {
		if err := os.WriteFile(filepath.Join(out, name+".md"), []byte(text), 0644); err != nil {
			panic(err)
		}
	}
	ctx := context.Background()
	p := provider.New("docs")()
	var schema ps.SchemaResponse
	p.Schema(ctx, ps.SchemaRequest{}, &schema)
	attrs := map[string]attribute{}
	for k, v := range schema.Schema.Attributes {
		attrs[k] = v
	}
	index := "# Terraform provider schema\n\nGenerated from the provider schemas; do not edit these tables manually.\n\n"
	index += "For installation, authentication, examples, imports and state protection, see the [Terraform guide](../../terraform.md).\n\n## Provider configuration\n\n" + schema.Schema.Description + "\n" + table(attrs) + "\n## Resources\n\n"
	var links []string
	for _, factory := range p.Resources(ctx) {
		r := factory()
		var meta resource.MetadataResponse
		r.Metadata(ctx, resource.MetadataRequest{ProviderTypeName: "fdp"}, &meta)
		var s resource.SchemaResponse
		r.Schema(ctx, resource.SchemaRequest{}, &s)
		if s.Diagnostics.HasError() {
			panic(s.Diagnostics)
		}
		attrs = map[string]attribute{}
		for k, v := range s.Schema.Attributes {
			attrs[k] = v
		}
		write(meta.TypeName, "# "+meta.TypeName+"\n\nGenerated from the current provider schema.\n\n"+s.Schema.Description+"\n"+table(attrs)+"\nNested object types are shown in the Type column. Server-side permission, layer and ownership checks also apply.\n")
		links = append(links, "- ["+meta.TypeName+"]("+meta.TypeName+".md)\n")
	}
	sort.Strings(links)
	index += strings.Join(links, "") + "\n## Data sources\n\n"
	for _, factory := range p.DataSources(ctx) {
		d := factory()
		var meta datasource.MetadataResponse
		d.Metadata(ctx, datasource.MetadataRequest{ProviderTypeName: "fdp"}, &meta)
		var s datasource.SchemaResponse
		d.Schema(ctx, datasource.SchemaRequest{}, &s)
		if s.Diagnostics.HasError() {
			panic(s.Diagnostics)
		}
		attrs = map[string]attribute{}
		for k, v := range s.Schema.Attributes {
			attrs[k] = v
		}
		name := "data_" + meta.TypeName
		write(name, "# Data source: "+meta.TypeName+"\n\nGenerated from the current provider schema.\n\n"+s.Schema.Description+"\n"+table(attrs))
		index += "- [" + meta.TypeName + "](" + name + ".md)\n"
	}
	write("index", index)
}
