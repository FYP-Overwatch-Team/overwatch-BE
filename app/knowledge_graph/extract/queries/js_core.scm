; Shared by JavaScript, TypeScript and TSX.
(class_declaration name: (_) @name) @definition.class
(function_declaration name: (_) @name) @definition.function
(generator_function_declaration name: (_) @name) @definition.function
(method_definition name: (_) @name) @definition.method

; const handler = () => {}  /  const handler = function () {}
(variable_declarator
  name: (identifier) @name
  value: [(arrow_function) (function_expression)]) @definition.function

(import_statement) @import
; `export { a } from "./x"` and `export * from "./x"`: a barrel passing names on
(export_statement source: (_)) @import
(export_statement "default") @default_export

(call_expression function: (_) @callee) @call
(new_expression constructor: (_) @callee) @construction
