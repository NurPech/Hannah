#!/usr/bin/env python3
import sys
import re

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
        
    gcode_path = sys.argv[1]
    
    try:
        try:
            with open(gcode_path, 'r', encoding='utf-8') as f:
                raw_lines = f.readlines()
        except UnicodeDecodeError:
            with open(gcode_path, 'r', encoding='latin-1') as f:
                raw_lines = f.readlines()
        
        modified_lines = []
        in_toolchange = False
        in_grid = False
        
        move_pattern = re.compile(r'^\s*G[0-3]\b', re.IGNORECASE)
        
        for line in raw_lines:
            clean_line = line.rstrip('\r\n')
            
            # 1. Erkennung der Toolchange-Zone
            if '; CP TOOLCHANGE START' in clean_line:
                in_toolchange = True
            elif '; CP TOOLCHANGE END' in clean_line:
                in_toolchange = False
                
            # 2. Erkennung der Grid-/Hüll-Zone des Turms
            if '; CP EMPTY GRID START' in clean_line:
                in_grid = True
            elif '; CP EMPTY GRID END' in clean_line:
                in_grid = False
            
            # Wenn wir uns in EINER der beiden Turm-Zonen befinden
            if (in_toolchange or in_grid) and move_pattern.match(clean_line):
                code_part = clean_line.split(';', 1)[0] if ';' in clean_line else clean_line
                
                if any(char in code_part.upper() for char in ['X', 'Y', 'Z', 'E']):
                    # Alten F-Wert eliminieren
                    clean_line = re.sub(r'[fF]\d+(\.\d+)?', '', clean_line).strip()
                    # Testgeschwindigkeit erzwingen
                    clean_line = f"{clean_line} F600"
            
            modified_lines.append(clean_line + '\n')
            
        with open(gcode_path, 'w', encoding='utf-8') as f:
            f.writelines(modified_lines)
            
    except Exception as e:
        pass

if __name__ == '__main__':
    main()