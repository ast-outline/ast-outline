import Foundation
import Combine

/// User data model
public struct User: Codable, Identifiable {
    public let id: UUID
    public var name: String
    public var email: String
}

/// Status enum with raw value
public enum Status: Int, CaseIterable {
    case active = 0
    case inactive
    case pending
}

/// User service protocol
public protocol UserServiceProtocol {
    func getUser(byId id: UUID) async throws -> User
    func saveUser(_ user: User) async throws
    var currentUser: User? { get }
}

/// Concrete user service implementation
@available(iOS 15, *)
@MainActor
public final class UserService: UserServiceProtocol {
    @Published
    public private(set) var currentUser: User?
    
    private let apiClient: URLSession
    private var cancellables = Set<AnyCancellable>()
    
    public init(apiClient: URLSession = .shared) {
        self.apiClient = apiClient
    }
    
    public func getUser(byId id: UUID) async throws -> User {
        return User(id: id, name: "", email: "")
    }
    
    public func saveUser(_ user: User) async throws {
        // Save logic
    }
    
    static func shared() -> UserService {
        return UserService()
    }
}

/// Extension for publisher-based API
extension UserService {
    func fetchUsers() -> AnyPublisher<[User], Error> {
        return Just([]).setFailureType(to: Error.self).eraseToAnyPublisher()
    }
}

/// Type alias for callback
public typealias UserCallback = (Result<User, Error>) -> Void

/// Global helper function
public func makeDefaultUser() -> User {
    return User(id: UUID(), name: "Default", email: "default@example.com")
}

/// Global constant
public let defaultTimeout: TimeInterval = 30.0
