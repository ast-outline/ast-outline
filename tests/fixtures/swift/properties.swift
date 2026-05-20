import Foundation

/// Property types demo class
class PropertyDemo {
    // Stored properties (fields)
    let constantValue: String = "fixed"
    var mutableValue: Int = 0
    private var _backing: String = ""
    
    // Computed property (property)
    var computed: String {
        get { return _backing }
        set { _backing = newValue }
    }
    
    // Read-only computed property (property)
    var readOnlyComputed: String {
        return "read only"
    }
    
    // Property with observer
    var observed: Int = 0 {
        didSet { print("changed") }
    }
    
    // Lazy property
    lazy var lazyValue: String = {
        return "lazy"
    }()
    
    // Subscript
    subscript(index: Int) -> String {
        get { return "\(index)" }
        set { }
    }
    
    // Deinit
    deinit {
        print("deinit")
    }
}
