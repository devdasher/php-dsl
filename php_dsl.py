#!/usr/bin/env python3
import re
import sys
import logging
import os
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)

class DSLConverter:
    def __init__(self):
        self.block_types = []
        self.known_vars = set()
        self.known_constants = set()
        self.indent_stack = [0]
        self.do_while_pending = False
        self.switch_pending = False
        self.case_pending = False
        self.php_lines = []
        self.block_handlers = defaultdict(list)
        self.current_match_var = None
        self.match_cases = []
        self.in_function = False
        self.in_anon_fn = False
        self.with_vars = {}
        self.current_match_type = None
        self.current_match_dest = None
        self._register_handlers()

    def _register_handlers(self):
        handlers = [
            (r'^import\s+"(.+)"$', self._handle_import),
            (r'^func\s+(\w+)\s*\((.+?)\)\s*(\w*)\s*:$', self._handle_func),
            (r'^(\w+)\s*=\s*fn\s*\((.+?)\)\s*:\s*(.+)$', self._handle_anon_fn_short),
            (r'^(\w+)\s*=\s*fn\s*\((.+?)\)\s*:$', self._handle_anon_fn_long),
            (r'^with\s+(.+?)\s+as\s+(\w+)\s*:$', self._handle_with),
            (r'^(\w+)\s*=\s*match\s+(\w+)\s*:.*$', self._handle_match_assignment),
            (r'^return\s+match\s+(\w+)\s*:.*$', self._handle_match_return),
            (r'^case\s+(.+?):\s*(.+)$', self._handle_case),
            (r'^default:\s*(.+)$', self._handle_default),
            (r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*input\s*\((.*)\)$', self._handle_input),
            (r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s+if\s+(.+?)\s+else\s+(.+)$', self._handle_ternary),
            (r'^do:$', self._handle_do),
            (r'^while\s+(.+):$', self._handle_while),
            (r'^while\s+(.+)$', self._handle_while),
            (r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.+)$', self._handle_constant),
            (r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.+)$', self._handle_assignment),
            (r'^return\s+(.+)$', self._handle_return),
            (r'^break$', self._handle_break),
            (r'^continue$', self._handle_continue),
            (r'^pass$', self._handle_pass),
            (r'^print\((.+)\)$', self._handle_print),
            (r'^if\s+(.+):$', self._handle_if),
            (r'^elif\s+(.+):$', self._handle_elif),
            (r'^else:$', self._handle_else),
            (r'^for\s+(\w+)\s+in\s+range\((.*?)\):$', self._handle_for_range),
            (r'^for\s+(\w+)\s+in\s+(.+):$', self._handle_foreach),
            (r'^switch\s+(.+):$', self._handle_switch),
            (r'^case\s+(.+):$', self._handle_switch_case),
            (r'^default:$', self._handle_switch_default),
            (r'^(\w+)\((.*)\)$', self._handle_function_call),
        ]
        for pattern, handler in handlers:
            self.block_handlers[0].append((re.compile(pattern), handler))

    def _adjust_indent(self, indent):
        while self.indent_stack and indent < self.indent_stack[-1]:
            level = self.indent_stack.pop()
            block_type = self.block_types.pop() if self.block_types else None
            
            if self.current_match_var:
                self._finalize_match()
                
            if block_type == 'do' and self.do_while_pending:
                continue
            
            if block_type == 'with':
                value = self.with_vars.get(level)
                if value:
                    base_level, var_name = value
                    self.php_lines.append(' ' * base_level + '} finally {')
                    self.php_lines.append(' ' * (base_level + 4) + f'if (isset(${var_name}) && is_resource(${var_name})) {{')
                    self.php_lines.append(' ' * (base_level + 8) + f'fclose(${var_name});')
                    self.php_lines.append(' ' * (base_level + 4) + '}')
                    self.php_lines.append(' ' * base_level + '}')
            elif block_type == 'anon_fn':
                self.php_lines.append(' ' * level + '};')
            elif self.case_pending:
                self.php_lines.append(' ' * level + 'break;')
                self.case_pending = False
                self.php_lines.append(' ' * level + '}')
            elif self.switch_pending:
                self.php_lines.append(' ' * level + '}')
                self.switch_pending = False
            else:
                self.php_lines.append(' ' * level + '}')

    def _convert_f_string(self, s):
        if s.startswith('f"'):
            content = s[2:-1]
        elif s.startswith("f'"):
            content = s[2:-1]
        else:
            return s
        
        parts = []
        current = []
        in_placeholder = False
        
        for char in content:
            if char == '{' and not in_placeholder:
                in_placeholder = True
                if current:
                    parts.append('"' + ''.join(current).replace('"', '\\"') + '"')
                    current = []
            elif char == '}' and in_placeholder:
                in_placeholder = False
                var_expr = ''.join(current).strip()
                var_expr = self._replace_vars(var_expr)
                parts.append(var_expr)
                current = []
            else:
                current.append(char)
        
        if current:
            if in_placeholder:
                var_expr = ''.join(current).strip()
                var_expr = self._replace_vars(var_expr)
                parts.append(var_expr)
            else:
                parts.append('"' + ''.join(current).replace('"', '\\"') + '"')
        
        return ' . '.join(parts) if len(parts) > 1 else parts[0]

    def _replace_vars(self, expr):
        # Convert Python methods to PHP equivalents
        expr = re.sub(r'(\w+)\.split\s*\(([^)]+)\)', r'explode(\2, \1)', expr)
        expr = re.sub(r'(\w+)\.strip\s*\(\)', r'trim(\1)', expr)
        expr = re.sub(r'(\w+)\.append\s*\(([^)]+)\)', r'\1[] = \2', expr)
        expr = expr.replace('len(', 'count(')
        expr = expr.replace('open(', 'fopen(')
        expr = expr.replace('int(', 'intval(')
        
        # Handle f-strings
        if expr.startswith('f"') or expr.startswith("f'"):
            return self._convert_f_string(expr)
            
        # Handle regular string concatenation
        expr = expr.replace('..', '.')
        
        # Split into string and non-string parts
        parts = []
        current = []
        in_string = False
        string_char = None
        
        for char in expr:
            if char in ('"', "'") and (not in_string or string_char == char):
                if in_string:
                    current.append(char)
                    parts.append(''.join(current))
                    current = []
                    in_string = False
                    string_char = None
                else:
                    if current:
                        parts.append(''.join(current))
                        current = []
                    current.append(char)
                    in_string = True
                    string_char = char
            else:
                current.append(char)
        
        if current:
            parts.append(''.join(current))
        
        # Process non-string parts
        result = []
        for part in parts:
            if (part.startswith('"') and part.endswith('"')) or (part.startswith("'") and part.endswith("'")):
                result.append(part)
            else:
                # Replace known variables
                part = re.sub(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', 
                              lambda m: f'${m.group(1)}' if m.group(1) in self.known_vars and m.group(1) not in self.known_constants 
                              else m.group(1) if m.group(1) in self.known_constants
                              else m.group(0), 
                              part)
                # Replace Python keywords
                part = part.replace('True', 'true')
                part = part.replace('False', 'false')
                part = part.replace('None', 'null')
                part = part.replace(' and ', ' && ')
                part = part.replace(' or ', ' || ')
                part = part.replace(' not ', ' ! ')
                result.append(part)
        
        return ''.join(result)

    def _convert_value(self, value):
        v = value.strip()
        if v.startswith('[') and v.endswith(']'):
            parts = [p.strip() for p in v[1:-1].split(',')]
            return '[' + ', '.join(self._replace_vars(p) for p in parts) + ']'
        if v.startswith('{') and v.endswith('}'):
            parts = [p.strip() for p in v[1:-1].split(',')]
            kv = []
            for p in parts:
                if ':' not in p: 
                    continue
                k, val = p.split(':', 1)
                key = k.strip().strip('"').strip("'")
                kv.append(f"'{key}' => {self._replace_vars(val.strip())}")
            return '[' + ', '.join(kv) + ']'
        low = v.lower()
        if low == 'true': 
            return 'true'
        if low == 'false': 
            return 'false'
        if v.isdigit(): 
            return v
        return self._replace_vars(v)

    def _handle_import(self, m, indent):
        filename = m.group(1)
        if filename.endswith('.ephp'):
            php_file = filename.replace('.ephp', '.php')
            self.php_lines.append(' ' * indent + f'require_once "{php_file}";')
        else:
            self.php_lines.append(' ' * indent + f'require_once "{filename}";')
        return True

    def _handle_func(self, m, indent):
        name, args_str, return_type = m.groups()
        return_type_map = {
            'void': 'void',
            'int': 'int',
            'str': 'string',
            'bool': 'bool',
            'float': 'float'
        }
        php_return_type = return_type_map.get(return_type, return_type)
        
        arg_parts = []
        arg_names = []
        if args_str.strip():
            for arg in args_str.split(','):
                arg = arg.strip()
                if not arg:
                    continue
                    
                if '=' in arg:
                    arg_name, default_val = arg.split('=', 1)
                    arg_name = arg_name.strip()
                    default_val = default_val.strip()
                    
                    # Extract type if exists
                    if ' ' in arg_name:
                        type_part, var_name = arg_name.rsplit(' ', 1)
                        php_type = return_type_map.get(type_part, type_part)
                        # Convert 'list' to 'array'
                        if php_type == 'list':
                            php_type = 'array'
                        arg_parts.append(f'{php_type} ${var_name} = {self._replace_vars(default_val)}')
                        arg_names.append(var_name)
                    else:
                        # Convert 'list' to 'array'
                        if arg_name == 'list':
                            arg_name = 'array'
                        arg_parts.append(f'${arg_name} = {self._replace_vars(default_val)}')
                        arg_names.append(arg_name)
                else:
                    if ' ' in arg:
                        type_part, var_name = arg.rsplit(' ', 1)
                        php_type = return_type_map.get(type_part, type_part)
                        # Convert 'list' to 'array'
                        if php_type == 'list':
                            php_type = 'array'
                        arg_parts.append(f'{php_type} ${var_name}')
                        arg_names.append(var_name)
                    else:
                        # Convert 'list' to 'array'
                        if arg == 'list':
                            arg = 'array'
                        arg_parts.append(f'${arg}')
                        arg_names.append(arg)
        
        ph_args = ', '.join(arg_parts)
        self.php_lines.append(' ' * indent + f'function {name}({ph_args}): {php_return_type} {{')
        self.indent_stack.append(indent + 4)
        self.block_types.append('function')
        
        # Add function arguments to known variables
        for arg in arg_names:
            self.known_vars.add(arg)
        return True

    def _handle_anon_fn_short(self, m, indent):
        var_name, args, body = m.groups()
        arg_parts = []
        arg_names = []
        if args.strip():
            for arg in args.split(','):
                arg = arg.strip()
                if ' ' in arg:
                    type_part, arg_name = arg.split(' ', 1)
                    arg_parts.append(f'{type_part} ${arg_name}')
                    arg_names.append(arg_name)
                else:
                    arg_parts.append(f'${arg}')
                    arg_names.append(arg)
        ph_args = ', '.join(arg_parts)
        
        # Remove 'return' if present
        if body.startswith('return '):
            body = body[7:]
        
        # Add arguments to known vars
        for arg in arg_names:
            self.known_vars.add(arg)
            
        body = self._replace_vars(body)
        self.php_lines.append(' ' * indent + f'${var_name} = fn({ph_args}) => {body};')
        self.known_vars.add(var_name)
        return True

    def _handle_anon_fn_long(self, m, indent):
        var_name, args = m.groups()
        arg_parts = []
        arg_names = []
        if args.strip():
            for arg in args.split(','):
                arg = arg.strip()
                if ' ' in arg:
                    type_part, arg_name = arg.split(' ', 1)
                    arg_parts.append(f'{type_part} ${arg_name}')
                    arg_names.append(arg_name)
                else:
                    arg_parts.append(f'${arg}')
                    arg_names.append(arg)
        ph_args = ', '.join(arg_parts)
        
        # Add arguments to known vars
        for arg in arg_names:
            self.known_vars.add(arg)
            
        self.php_lines.append(' ' * indent + f'${var_name} = function({ph_args}) {{')
        self.indent_stack.append(indent + 4)
        self.block_types.append('anon_fn')
        self.known_vars.add(var_name)
        return True

    def _handle_with(self, m, indent):
        expr, var_name = m.groups()
        php_expr = self._replace_vars(expr)
        self.php_lines.append(' ' * indent + f'${var_name} = {php_expr};')
        self.php_lines.append(' ' * indent + 'try {')
        self.indent_stack.append(indent + 4)
        self.block_types.append('with')
        self.known_vars.add(var_name)
        self.with_vars[indent + 4] = (indent, var_name)
        return True

    def _handle_match_assignment(self, m, indent):
        var_name, match_var = m.groups()
        self.current_match_dest = var_name
        self.current_match_type = 'assignment'
        self.current_match_var = match_var
        self.match_cases = []
        self.known_vars.add(var_name)
        return True

    def _handle_match_return(self, m, indent):
        match_var = m.group(1)
        self.current_match_dest = None
        self.current_match_type = 'return'
        self.current_match_var = match_var
        self.match_cases = []
        return True

    def _handle_case(self, m, indent):
        if not self.current_match_var:
            return False
            
        values, result = m.groups()
        # Split and process values
        value_list = []
        for v in values.split('or'):
            for part in v.split('and'):
                value_list.append(part.strip())
        
        self.match_cases.append({
            'values': [self._replace_vars(v) for v in value_list],
            'result': self._replace_vars(result.strip())
        })
        return True

    def _handle_default(self, m, indent):
        if not self.current_match_var:
            return False
            
        result = m.group(1).strip()
        self.match_cases.append({
            'values': ['default'],
            'result': self._replace_vars(result)
        })
        return True

    def _finalize_match(self):
        if not self.current_match_var or not self.match_cases:
            return
            
        indent = self.indent_stack[-1] if self.indent_stack else 0
        
        # Only add $ prefix for variable names, not for boolean values
        if self.current_match_var.lower() in ['true', 'false']:
            expr_str = self.current_match_var
        else:
            expr_str = f'${self.current_match_var}'
        
        if self.current_match_type == 'assignment':
            php_lines = [f'${self.current_match_dest} = match ({expr_str}) {{']
        elif self.current_match_type == 'return':
            php_lines = [f'return match ({expr_str}) {{']
        else:
            return
        
        for case in self.match_cases:
            if case['values'] == ['default']:
                php_lines.append('    ' + f'default => {case["result"]},')
            else:
                # Join the values with commas
                values = ', '.join(case['values'])
                php_lines.append('    ' + f'{values} => {case["result"]},')
        
        php_lines.append('};')
        self.php_lines.extend([' ' * indent + line for line in php_lines])
        
        self.current_match_var = None
        self.match_cases = []
        self.current_match_type = None
        self.current_match_dest = None
    def _handle_input(self, m, indent):
        var, prompt = m.groups()
        prompt = prompt.strip().strip('"').strip("'")
        self.php_lines.append(' ' * indent + f'${var} = readline("{prompt}");')
        self.known_vars.add(var)
        return True

    def _handle_ternary(self, m, indent):
        var, tv, cond, fv = m.groups()
        self.known_vars.add(var)
        c = self._replace_vars(cond)
        t = self._replace_vars(tv)
        f = self._replace_vars(fv)
        self.php_lines.append(' ' * indent + f'${var} = ({c}) ? {t} : {f};')
        return True

    def _handle_constant(self, m, indent):
        n, v = m.groups()
        php_v = self._replace_vars(v)
        self.php_lines.append(' ' * indent + f'define("{n}", {php_v});')
        self.known_constants.add(n)
        return True

    def _handle_assignment(self, m, indent):
        var, val = m.groups()
        self.known_vars.add(var)
        php_v = self._convert_value(val)
        self.php_lines.append(' ' * indent + f'${var} = {php_v};')
        return True

    def _handle_return(self, m, indent):
        self.php_lines.append(' ' * indent + f'return {self._replace_vars(m.group(1))};')
        return True

    def _handle_break(self, m, indent):
        self.php_lines.append(' ' * indent + 'break;')
        return True

    def _handle_continue(self, m, indent):
        self.php_lines.append(' ' * indent + 'continue;')
        return True

    def _handle_pass(self, m, indent):
        self.php_lines.append(' ' * indent + '// pass')
        return True

    def _handle_print(self, m, indent):
        content = m.group(1)
        # Handle array printing with implode
        if re.search(r'\$[a-zA-Z_][a-zA-Z0-9_]*$', content):
            content = f'implode(", ", {content})'
        self.php_lines.append(' ' * indent + f'echo {self._replace_vars(content)};')
        return True

    def _handle_if(self, m, indent):
        cond = self._replace_vars(m.group(1))
        self.php_lines.append(' ' * indent + f'if ({cond}) {{')
        self.indent_stack.append(indent + 4)
        self.block_types.append('if')
        return True

    def _handle_elif(self, m, indent):
        cond = self._replace_vars(m.group(1))
        self.php_lines.append(' ' * indent + f'elseif ({cond}) {{')
        self.indent_stack.append(indent + 4)
        self.block_types.append('elif')
        return True

    def _handle_else(self, m, indent):
        self.php_lines.append(' ' * indent + 'else {')
        self.indent_stack.append(indent + 4)
        self.block_types.append('else')
        return True

    def _handle_for_range(self, m, indent):
        var, args = m.groups()
        parts = [p.strip() for p in args.split(',')]
        if len(parts) == 1:
            s, e, st = '0', parts[0], '1'
        elif len(parts) == 2:
            s, e, st = parts[0], parts[1], '1'
        else:
            s, e, st = parts[:3]
        for_line = f"for (${var} = {self._replace_vars(s)}; ${var} < {self._replace_vars(e)}; ${var} += {self._replace_vars(st)}) {{"
        self.php_lines.append(' ' * indent + for_line)
        self.indent_stack.append(indent + 4)
        self.block_types.append('for')
        self.known_vars.add(var)
        return True

    def _handle_foreach(self, m, indent):
        var, coll = m.groups()
        self.known_vars.add(var)
        self.php_lines.append(' ' * indent + f'foreach ({self._replace_vars(coll)} as ${var}) {{')
        self.indent_stack.append(indent + 4)
        self.block_types.append('foreach')
        return True

    def _handle_do(self, m, indent):
        self.php_lines.append(' ' * indent + 'do {')
        self.indent_stack.append(indent + 4)
        self.block_types.append('do')
        self.do_while_pending = True
        return True

    def _handle_while(self, m, indent):
        cond = m.group(1).strip()
        if self.do_while_pending:
            if self.indent_stack:
                lvl = self.indent_stack.pop()
                block_type = self.block_types.pop() if self.block_types else None
            self.php_lines.append(' ' * (lvl - 4) + f'}} while ({self._replace_vars(cond)});')
            self.do_while_pending = False
        else:
            self.php_lines.append(' ' * indent + f'while ({self._replace_vars(cond)}) {{')
            self.indent_stack.append(indent + 4)
            self.block_types.append('while')
        return True

    def _handle_switch(self, m, indent):
        self.php_lines.append(' ' * indent + f'switch ({self._replace_vars(m.group(1))}) {{')
        self.indent_stack.append(indent + 4)
        self.block_types.append('switch')
        self.switch_pending = True
        return True

    def _handle_switch_case(self, m, indent):
        if self.case_pending:
            self.php_lines.append(' ' * (indent - 4) + 'break;')
        self.php_lines.append(' ' * indent + f'case {self._replace_vars(m.group(1))}:')
        self.case_pending = True
        return True

    def _handle_switch_default(self, m, indent):
        if self.case_pending:
            self.php_lines.append(' ' * (indent - 4) + 'break;')
        self.php_lines.append(' ' * indent + 'default:')
        self.case_pending = True
        return True

    def _handle_function_call(self, m, indent):
        fn, args = m.groups()
        # Special handling for PHP functions
        if fn == 'fwrite':
            args = re.sub(r'f"(.+?)"', r'"\1"', args)
        self.php_lines.append(' ' * indent + f'{fn}({self._replace_vars(args)});')
        return True

    def convert(self, code):
        self.php_lines = ['<?php', '// Generated by DSL to PHP converter']
        self.indent_stack = [0]
        self.block_types = []
        self.do_while_pending = False
        self.current_match_var = None
        self.match_cases = []
        self.in_function = False
        self.in_anon_fn = False
        self.with_vars = {}
        self.known_vars = set()
        self.known_constants = set()
        self.current_match_type = None
        self.current_match_dest = None
        
        lines = code.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            raw = line.rstrip()
            indent = len(line) - len(line.lstrip())
            
            # Skip empty lines
            if not raw.strip():
                self.php_lines.append('')
                i += 1
                continue
                
            # Handle comments
            if raw.strip().startswith('#'):
                comment = raw.strip()[1:].strip()
                self.php_lines.append(' ' * indent + f'// {comment}')
                i += 1
                continue
                
            self._adjust_indent(indent)
            
            done = False
            for pattern, handler in self.block_handlers[0]:
                m = pattern.match(raw.strip())
                if m:
                    done = handler(m, indent)
                    break
                    
            if not done:
                if raw.strip().endswith(':'):
                    self.php_lines.append(' ' * indent + raw.strip()[:-1] + ' {')
                    self.indent_stack.append(indent + 4)
                    self.block_types.append('block')
                else:
                    self.php_lines.append(' ' * indent + self._replace_vars(raw.strip()) + ';')
            
            i += 1
        
        # Finalize any pending match
        if self.current_match_var:
            self._finalize_match()
        
        # Close remaining blocks
        while len(self.indent_stack) > 1:
            level = self.indent_stack.pop()
            block_type = self.block_types.pop() if self.block_types else None
            
            if self.current_match_var:
                self._finalize_match()
                
            if block_type == 'do' and self.do_while_pending:
                self.php_lines.append(' ' * (level - 4) + '} while (false);')
                self.do_while_pending = False
            elif block_type == 'with':
                value = self.with_vars.get(level)
                if value:
                    base_level, var_name = value
                    self.php_lines.append(' ' * base_level + '} finally {')
                    self.php_lines.append(' ' * (base_level + 4) + f'if (isset(${var_name}) && is_resource(${var_name})) {{')
                    self.php_lines.append(' ' * (base_level + 8) + f'fclose(${var_name});')
                    self.php_lines.append(' ' * (base_level + 4) + '}')
                    self.php_lines.append(' ' * base_level + '}')
            elif block_type == 'anon_fn':
                self.php_lines.append(' ' * level + '};')
            elif self.case_pending:
                self.php_lines.append(' ' * level + 'break;')
                self.case_pending = False
                self.php_lines.append(' ' * level + '}')
            elif self.switch_pending:
                self.php_lines.append(' ' * level + '}')
                self.switch_pending = False
            else:
                self.php_lines.append(' ' * level + '}')

        return "\n".join(self.php_lines)

def main():
    if len(sys.argv) < 2:
        print('Usage: python php_dsl.py <input.ephp> [output.php]')
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else inp.replace('.ephp', '.php')
    if not inp.endswith('.ephp'):
        print('Error: Input file must have .ephp extension')
        sys.exit(1)
        
    # Convert imported .ephp files first
    base_dir = os.path.dirname(inp)
    with open(inp, 'r', encoding='utf-8') as f:
        code = f.read()
        
    # Find all imports
    for line in code.splitlines():
        if line.strip().startswith('import'):
            match = re.match(r'^import\s+"(.+\.ephp)"$', line.strip())
            if match:
                import_file = match.group(1)
                import_path = os.path.join(base_dir, import_file)
                if os.path.exists(import_path):
                    print(f'Converting imported file: {import_path}')
                    import_out = import_path.replace('.ephp', '.php')
                    converter = DSLConverter()
                    with open(import_path, 'r', encoding='utf-8') as f_import:
                        import_code = f_import.read()
                    php_code = converter.convert(import_code)
                    with open(import_out, 'w', encoding='utf-8') as f_out:
                        f_out.write(php_code)
                else:
                    print(f'Warning: Imported file not found: {import_path}')
    
    # Convert main file
    print(f'Converting main file: {inp}')
    converter = DSLConverter()
    php = converter.convert(code)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(php)
        
    print(f'✅ Conversion successful! PHP file saved to: {out}')
    print(f'ℹ️ {len(php.splitlines())} lines generated')

if __name__ == '__main__':
    main()
