; Definitions. Kind is refined in post-processing: a function inside a class
; body becomes a method, and a module-level CONSTANT is filtered by name.
(class_definition name: (identifier) @name) @definition.class
(function_definition name: (identifier) @name) @definition.function
(assignment left: (identifier) @name) @definition.constant

; Imports
(import_statement) @import
(import_from_statement) @import

; Calls
(call function: (_) @callee) @call
