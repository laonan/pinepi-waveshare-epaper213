import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), 'python'))
from renderer import Renderer
from config import Config

def test():
    class DummyConfig:
        ap_ssid = "test"
        ap_password = "test"
    
    renderer = Renderer(DummyConfig())
    data = renderer.render_page2()
    print("Page 2 length:", len(data))
    
    # check if data is all white (0xFF)
    is_all_white = all(b == 255 for b in data)
    print("Is all white?", is_all_white)

    # Let's count bytes
    counts = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    print("Byte counts:", counts)

if __name__ == "__main__":
    test()
